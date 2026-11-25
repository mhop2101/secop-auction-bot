"""Tests for :mod:`secop_firstbid.auth` (plan §3).

Every token here is minted locally by :func:`make_token`: a base64url header, a base64url
JSON payload, and a signature segment that is nonsense on purpose. Nothing reads the HAR and
no real credential appears in this file — the module under test never verifies a signature,
so a fake token exercises it exactly as a real one would.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from secop_firstbid import auth
from secop_firstbid.auth import CLAIM_PREFIX, TokenInfo
from secop_firstbid.errors import Failure, SecopError

NOW = datetime(2026, 7, 28, 21, 0, 0, tzinfo=timezone.utc)
NBF = NOW - timedelta(hours=1)
EXP = NBF + timedelta(seconds=86400)  # the observed lifetime: exp - nbf == 86400

COMPANY_CODE = "999000111"
USER_CODE = "4242424"  # distinct from the company code, as in the real token
COMPANY_NAME = "UT FAKE TEST CONSORCIO 2026"
USER_ID = "12345"
USER_NAME = "fake.operator"
LOGIN_GUID = "00000000-0000-4000-8000-000000000001"
AUD = "https://subastas.secop.gov.co/eauctions.site/"
ISS = "https://community.secop.gov.co/STS/users/issue.aspx"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_token(
    payload: Dict[str, Any],
    header: Optional[Dict[str, Any]] = None,
    signature: str = "not-a-real-signature",
) -> str:
    """Mint an unsigned, locally-fabricated JWT. The signature segment is decorative."""
    head = {"alg": "none", "typ": "JWT"} if header is None else header
    return ".".join(
        (
            _b64url(json.dumps(head).encode("utf-8")),
            _b64url(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            _b64url(signature.encode("utf-8")),
        )
    )


def full_payload(exp: datetime = EXP, nbf: datetime = NBF) -> Dict[str, Any]:
    """A payload shaped like the real one: 7 bare claims, the rest under the STS prefix."""
    return {
        "iss": ISS,
        "aud": AUD,
        "nbf": int(nbf.timestamp()),
        "exp": int(exp.timestamp()),
        "unique_name": USER_NAME,
        "authmethod": "password",
        "auth_time": int(nbf.timestamp()),
        CLAIM_PREFIX + "LoggedInCompanyCode": COMPANY_CODE,
        CLAIM_PREFIX + "LoggedInCompanyName": COMPANY_NAME,
        CLAIM_PREFIX + "LoggedInUserCode": USER_CODE,
        CLAIM_PREFIX + "LoggedInUserId": USER_ID,
        CLAIM_PREFIX + "LoggedInUserName": USER_NAME,
        CLAIM_PREFIX + "LoginGUID": LOGIN_GUID,
    }


FULL_TOKEN = make_token(full_payload())


def test_decode_reads_the_payload_and_ignores_the_signature() -> None:
    tampered = make_token(full_payload(), signature="garbage-that-verifies-nothing")
    claims = auth.decode_jwt_claims(tampered)
    assert claims["aud"] == AUD
    assert claims[CLAIM_PREFIX + "LoggedInCompanyCode"] == COMPANY_CODE


@pytest.mark.parametrize("filler_len", list(range(0, 9)))
def test_padding_is_restored_for_every_segment_length(filler_len: int) -> None:
    payload = {"exp": int(EXP.timestamp()), "pad": "x" * filler_len}
    token = make_token(payload)
    assert len(token.split(".")[1]) % 4 != 1
    assert auth.decode_jwt_claims(token)["pad"] == "x" * filler_len


def test_payload_is_decoded_as_utf8() -> None:
    payload = full_payload()
    payload[CLAIM_PREFIX + "LoggedInCompanyName"] = "UNIÓN TEMPORAL ÑANDÚ"
    info = auth.inspect_token(make_token(payload), now=NOW)
    assert info.company_name == "UNIÓN TEMPORAL ÑANDÚ"


def test_loggedin_claims_are_uris_so_the_bare_key_raises() -> None:
    claims = auth.decode_jwt_claims(FULL_TOKEN)
    with pytest.raises(KeyError):
        claims["LoggedInCompanyCode"]
    assert auth.claim(claims, "LoggedInCompanyCode") == COMPANY_CODE


def test_bare_claims_are_found_without_the_prefix() -> None:
    claims = auth.decode_jwt_claims(FULL_TOKEN)
    for name in auth.BARE_CLAIM_NAMES:
        assert name in claims
        assert auth.claim(claims, name) == claims[name]
    assert CLAIM_PREFIX + "exp" not in claims


def test_claim_accepts_an_already_prefixed_name() -> None:
    claims = auth.decode_jwt_claims(FULL_TOKEN)
    assert auth.claim(claims, CLAIM_PREFIX + "LoginGUID") == LOGIN_GUID


def test_claim_returns_none_when_absent() -> None:
    assert auth.claim({"exp": 1}, "LoggedInCompanyCode") is None


@pytest.mark.parametrize("falsy", [0, "", False])
def test_a_falsy_bare_claim_does_not_fall_through_to_the_prefixed_one(
    falsy: Any,
) -> None:
    claims = {"LoggedInUserId": falsy, CLAIM_PREFIX + "LoggedInUserId": "shadow"}
    assert auth.claim(claims, "LoggedInUserId") == falsy


def test_exp_and_nbf_parse_to_tz_aware_utc() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    assert info.exp == EXP
    assert info.nbf == NBF
    assert info.exp.tzinfo is not None and info.exp.utcoffset() == timedelta(0)
    assert info.nbf.tzinfo is not None and info.nbf.utcoffset() == timedelta(0)
    assert (info.exp - info.nbf).total_seconds() == 86400


def test_numeric_string_timestamps_parse() -> None:
    payload = full_payload()
    payload["exp"] = str(int(EXP.timestamp()))
    payload["nbf"] = str(int(NBF.timestamp()))
    info = auth.inspect_token(make_token(payload), now=NOW)
    assert info.exp == EXP
    assert info.nbf == NBF


@pytest.mark.parametrize("bad", ["not-a-number", True, None, {"a": 1}, [1]])
def test_unparseable_exp_yields_none(bad: Any) -> None:
    payload = full_payload()
    payload["exp"] = bad
    info = auth.inspect_token(make_token(payload), now=NOW)
    assert info.exp is None
    assert info.seconds_remaining is None


def test_seconds_remaining_is_measured_from_the_injected_now() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    assert info.seconds_remaining == pytest.approx(82800.0)
    later = auth.inspect_token(FULL_TOKEN, now=EXP - timedelta(seconds=30))
    assert later.seconds_remaining == pytest.approx(30.0)
    past = auth.inspect_token(FULL_TOKEN, now=EXP + timedelta(seconds=45))
    assert past.seconds_remaining == pytest.approx(-45.0)


def test_naive_now_is_rejected() -> None:
    with pytest.raises(SecopError) as excinfo:
        auth.inspect_token(FULL_TOKEN, now=datetime(2026, 7, 28, 21, 0, 0))
    assert excinfo.value.code is Failure.CONFIG_INVALID


def test_company_code_is_the_audit_log_user_code_not_loggedin_user_code() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    assert info.company_code == COMPANY_CODE
    assert info.company_code != USER_CODE
    assert info.user_id == USER_ID
    assert info.company_name == COMPANY_NAME
    assert info.login_guid == LOGIN_GUID
    assert info.aud == AUD
    assert info.iss == ISS


def test_user_name_falls_back_to_the_bare_unique_name() -> None:
    payload = full_payload()
    del payload[CLAIM_PREFIX + "LoggedInUserName"]
    payload["unique_name"] = "fallback.operator"
    info = auth.inspect_token(make_token(payload), now=NOW)
    assert info.user_name == "fallback.operator"


def test_raw_claim_names_are_verbatim_uris_in_payload_order() -> None:
    payload = full_payload()
    info = auth.inspect_token(make_token(payload), now=NOW)
    assert info.raw_claim_names == list(payload.keys())
    assert CLAIM_PREFIX + "LoggedInCompanyCode" in info.raw_claim_names
    assert "LoggedInCompanyCode" not in info.raw_claim_names


def test_preflight_passes_with_time_to_spare() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    assert auth.preflight_token(info, 300) is None


def test_preflight_raises_auth_expired_past_exp() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=EXP + timedelta(seconds=1))
    with pytest.raises(SecopError) as excinfo:
        auth.preflight_token(info, 300)
    assert excinfo.value.code is Failure.AUTH_EXPIRED


def test_preflight_raises_auth_expired_exactly_at_exp() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=EXP)
    assert info.seconds_remaining == 0.0
    with pytest.raises(SecopError) as excinfo:
        auth.preflight_token(info, 300)
    assert excinfo.value.code is Failure.AUTH_EXPIRED


def test_preflight_raises_auth_expiring_inside_the_margin() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=EXP - timedelta(seconds=299))
    with pytest.raises(SecopError) as excinfo:
        auth.preflight_token(info, 300)
    assert excinfo.value.code is Failure.AUTH_EXPIRING
    assert excinfo.value.detail["safety_margin_s"] == 300


def test_preflight_margin_boundary_is_inclusive() -> None:
    at_margin = auth.inspect_token(FULL_TOKEN, now=EXP - timedelta(seconds=300))
    with pytest.raises(SecopError) as excinfo:
        auth.preflight_token(at_margin, 300)
    assert excinfo.value.code is Failure.AUTH_EXPIRING

    just_outside = auth.inspect_token(FULL_TOKEN, now=EXP - timedelta(seconds=301))
    assert auth.preflight_token(just_outside, 300) is None


def test_preflight_fails_closed_without_an_exp_claim() -> None:
    payload = full_payload()
    del payload["exp"]
    info = auth.inspect_token(make_token(payload), now=NOW)
    assert info.exp is None
    with pytest.raises(SecopError) as excinfo:
        auth.preflight_token(info, 300)
    assert excinfo.value.code is Failure.CONFIG_INVALID


def test_repr_contains_no_part_of_the_token() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    text = repr(info)
    assert FULL_TOKEN not in text
    for segment in FULL_TOKEN.split("."):
        assert segment not in text
    assert text.startswith("TokenInfo(")
    assert COMPANY_CODE in text


def test_repr_is_the_explicit_one_not_the_dataclass_default() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    text = repr(info)
    assert "claims=13" in text
    assert "raw_claim_names=" not in text


def test_repr_survives_a_token_with_nothing_in_it() -> None:
    info = auth.inspect_token(make_token({}), now=NOW)
    assert repr(info) == (
        "TokenInfo(exp=None, nbf=None, seconds_remaining=None, aud=None, iss=None, "
        "company_code=None, company_name=None, user_id=None, user_name=None, "
        "login_guid=None, claims=0)"
    )


def test_token_info_is_frozen() -> None:
    info = auth.inspect_token(FULL_TOKEN, now=NOW)
    assert isinstance(info, TokenInfo)
    with pytest.raises(Exception):
        info.company_code = "tampered"  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "onlyonesegment",
        "two.segments",
        "a.b.c.d",
        "eyJhbGciOiJub25lIn0.!!!not-base64!!!.sig",
        ".".join(("aGVhZGVy", _b64url(b"not json at all"), "sig")),
        ".".join(("aGVhZGVy", _b64url(json.dumps([1, 2, 3]).encode()), "sig")),
        ".".join(("aGVhZGVy", _b64url(json.dumps("a string").encode()), "sig")),
        ".".join(("aGVhZGVy", _b64url(b"\xff\xfe\xff"), "sig")),
    ],
)
def test_malformed_tokens_are_config_errors(bad: str) -> None:
    with pytest.raises(SecopError) as excinfo:
        auth.decode_jwt_claims(bad)
    assert excinfo.value.code is Failure.CONFIG_INVALID


def test_a_pasted_bearer_prefix_is_named_in_the_error() -> None:
    with pytest.raises(SecopError) as excinfo:
        auth.decode_jwt_claims("Bearer " + FULL_TOKEN)
    assert excinfo.value.code is Failure.CONFIG_INVALID
    assert "Bearer" in excinfo.value.message


def test_auth_header() -> None:
    assert auth.auth_header(FULL_TOKEN) == {"Authorization": "Bearer " + FULL_TOKEN}


def test_auth_header_strips_a_pasted_trailing_newline() -> None:
    header = auth.auth_header("  " + FULL_TOKEN + "\n")
    assert header["Authorization"] == "Bearer " + FULL_TOKEN
    assert "\n" not in header["Authorization"]
