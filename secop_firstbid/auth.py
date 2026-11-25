"""JWT claim reading, token preflight, and the Bearer header (plan §3).

The operator pastes the token; this module only *reads* it. Nothing here authenticates
anything: the signature is never checked, no key is fetched, and a forged token would sail
straight through :func:`decode_jwt_claims`. The server at ``subastas.secop.gov.co`` is the
sole authority on whether the token is good, and it says so with ``401``. The reason to
decode the payload locally is narrower: to log *which* identity is about to bid, and to
refuse to walk into a fire instant with a token whose ``exp`` has already passed — a
network round trip we can skip is a round trip that cannot be slow at ``T``.

Claim names are the trap here. Only ``iss``, ``aud``, ``nbf``, ``exp``, ``unique_name``,
``authmethod`` and ``auth_time`` are bare keys. All 15 ``LoggedIn*``/``LoginGUID`` claims
are minted under :data:`CLAIM_PREFIX`, so ``claims["LoggedInCompanyCode"]`` raises
``KeyError``; go through :func:`claim`, which tries the bare name and then the prefixed one.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from .errors import Failure, SecopError

CLAIM_PREFIX = "http://www.nextway.com/NextWay.STS.Claim/"

BARE_CLAIM_NAMES = (
    "iss",
    "aud",
    "nbf",
    "exp",
    "unique_name",
    "authmethod",
    "auth_time",
)


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except ValueError as exc:  # binascii.Error and UnicodeEncodeError both subclass it
        raise SecopError(
            Failure.CONFIG_INVALID,
            "jwt payload segment is not valid base64url: %s" % (exc,),
            {"segment_len": len(segment)},
        )


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Return the claims from a JWT's payload segment. **The signature is not verified.**

    This is claim reading, not authentication — no key is consulted and no ``alg`` is
    honoured, so the result proves nothing about the token's authenticity and must never be
    used to decide that a token is trustworthy. Only the server can decide that.

    The payload is base64url with its ``=`` padding stripped, so the padding is restored
    before decoding. A token that is not three dot-separated segments, whose payload is not
    base64url, or whose payload is not a JSON object raises ``SecopError(CONFIG_INVALID)``:
    the token is a §4 config field, and a pasted value of the wrong shape is a config
    mistake the operator has to fix before anything else can run.
    """
    if not isinstance(token, str) or not token.strip():
        raise SecopError(
            Failure.CONFIG_INVALID,
            "jwt is required: paste the raw token from the auction page "
            "(the value of data-item-token, or the token= query param)",
            {"jwt_len": len(token) if isinstance(token, str) else None},
        )
    text = token.strip()
    if text[:7].lower() == "bearer ":
        raise SecopError(
            Failure.CONFIG_INVALID,
            "jwt must be the bare token, without the 'Bearer ' prefix "
            "(auth_header adds it)",
            {"jwt_len": len(text)},
        )
    parts = text.split(".")
    if len(parts) != 3:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "jwt must have 3 dot-separated segments, got %d" % (len(parts),),
            {"segments": len(parts), "jwt_len": len(text)},
        )
    raw = _b64url_decode(parts[1])
    try:
        claims = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "jwt payload is not utf-8 JSON: %s" % (exc,),
            {"payload_bytes": len(raw)},
        )
    if not isinstance(claims, dict):
        raise SecopError(
            Failure.CONFIG_INVALID,
            "jwt payload decoded to %s, expected a JSON object of claims"
            % (type(claims).__name__,),
        )
    return claims


def claim(claims: Mapping[str, Any], name: str) -> Optional[Any]:
    """Look a claim up by bare name, falling back to the :data:`CLAIM_PREFIX` URI form.

    ``LoggedInCompanyCode``, ``LoggedInUserCode``, ``LoggedInUserId``, ``LoginGUID`` and the
    rest of that family exist only under the prefix; ``exp``/``nbf``/``aud``/``iss`` exist
    only bare. Passing an already-prefixed name also works — the bare lookup finds it.
    """
    # Membership, not `or`: a claim whose value is 0, "" or False must be returned as-is,
    # never fall through to the prefixed name.
    if name in claims:
        return claims[name]
    prefixed = CLAIM_PREFIX + name
    if prefixed in claims:
        return claims[prefixed]
    return None


def _claim_str(claims: Mapping[str, Any], name: str) -> Optional[str]:
    value = claim(claims, name)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _claim_datetime(claims: Mapping[str, Any], name: str) -> Optional[datetime]:
    value = claim(claims, name)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


@dataclass(frozen=True)
class TokenInfo:
    """Identity and lifetime read off the token, for logging and preflight.

    ``company_code`` is ``LoggedInCompanyCode`` — the same value the server writes as
    ``userCode`` on the buyer-visible ``Logs_Search_NewBid`` audit row, i.e. the number that
    identifies you in the auction's event log. It is *not* ``LoggedInUserCode``, a distinct
    claim the same token also carries; they are different numbers and only the company code
    ties back to the audit trail.

    ``seconds_remaining`` is a snapshot taken at :func:`inspect_token` time, not a live
    countdown: re-inspect before arming if the token was inspected long before ``T``.

    The raw token is deliberately not a field, and :meth:`__repr__` is written out by hand
    rather than left to the dataclass default so that a field added later cannot start
    leaking a credential into logs and tracebacks.
    """

    exp: Optional[datetime]
    nbf: Optional[datetime]
    aud: Optional[str]
    iss: Optional[str]
    company_code: Optional[str]
    company_name: Optional[str]
    user_id: Optional[str]
    user_name: Optional[str]
    login_guid: Optional[str]
    seconds_remaining: Optional[float]
    raw_claim_names: List[str]

    def __repr__(self) -> str:
        def stamp(value: Optional[datetime]) -> str:
            return "None" if value is None else value.isoformat()

        return (
            "TokenInfo(exp=%s, nbf=%s, seconds_remaining=%s, aud=%r, iss=%r, "
            "company_code=%r, company_name=%r, user_id=%r, user_name=%r, "
            "login_guid=%r, claims=%d)"
            % (
                stamp(self.exp),
                stamp(self.nbf),
                self.seconds_remaining,
                self.aud,
                self.iss,
                self.company_code,
                self.company_name,
                self.user_id,
                self.user_name,
                self.login_guid,
                len(self.raw_claim_names),
            )
        )


def inspect_token(token: str, now: Optional[datetime] = None) -> TokenInfo:
    """Decode the token and summarise it. Does not verify the signature or contact anyone.

    ``raw_claim_names`` keeps the payload's own key order and the full URI of every prefixed
    claim, so the log records what the token actually contained rather than this module's
    idea of it. ``user_name`` prefers ``LoggedInUserName`` and falls back to the bare
    ``unique_name``.
    """
    claims = decode_jwt_claims(token)
    if now is None:
        moment = datetime.now(timezone.utc)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "inspect_token(now=...) must be tz-aware; a naive instant cannot be compared "
            "with the token's unix exp",
        )
    else:
        moment = now
    exp = _claim_datetime(claims, "exp")
    seconds_remaining = None
    if exp is not None:
        seconds_remaining = (exp - moment).total_seconds()
    user_name = _claim_str(claims, "LoggedInUserName")
    if user_name is None:
        user_name = _claim_str(claims, "unique_name")
    return TokenInfo(
        exp=exp,
        nbf=_claim_datetime(claims, "nbf"),
        aud=_claim_str(claims, "aud"),
        iss=_claim_str(claims, "iss"),
        company_code=_claim_str(claims, "LoggedInCompanyCode"),
        company_name=_claim_str(claims, "LoggedInCompanyName"),
        user_id=_claim_str(claims, "LoggedInUserId"),
        user_name=user_name,
        login_guid=_claim_str(claims, "LoginGUID"),
        seconds_remaining=seconds_remaining,
        raw_claim_names=list(claims.keys()),
    )


def preflight_token(info: TokenInfo, safety_margin_s: int) -> None:
    """Refuse to proceed on a token that is dead or about to die. Returns ``None`` on pass.

    Raises ``AUTH_EXPIRED`` at or past ``exp`` (RFC 7519: a JWT must not be accepted *on or
    after* its expiry), ``AUTH_EXPIRING`` when less than ``safety_margin_s`` is left, and
    ``CONFIG_INVALID`` when the token carries no readable ``exp`` at all — an unverifiable
    lifetime fails closed rather than silently passing.

    This is arithmetic on a claim, not proof the token works: it cannot see a revoked
    session or a server that stopped trusting the issuer. Only the §3 authenticated
    ``GetAuctionSimple`` proves liveness, and a ``401`` anywhere still means re-harvest.
    ``nbf`` is read for the log but not enforced here.
    """
    if info.exp is None or info.seconds_remaining is None:
        raise SecopError(
            Failure.CONFIG_INVALID,
            "token carries no readable 'exp' claim; its lifetime cannot be checked",
            {"claims": len(info.raw_claim_names)},
        )
    remaining = info.seconds_remaining
    if remaining <= 0:
        raise SecopError(
            Failure.AUTH_EXPIRED,
            "token expired %.0f s ago (exp %s); harvest a fresh one"
            % (-remaining, info.exp.isoformat()),
            {"exp": info.exp.isoformat(), "seconds_remaining": remaining},
        )
    if remaining <= safety_margin_s:
        raise SecopError(
            Failure.AUTH_EXPIRING,
            "token has %.0f s left, under the %d s safety margin (exp %s); harvest a "
            "fresh one before arming"
            % (remaining, safety_margin_s, info.exp.isoformat()),
            {
                "exp": info.exp.isoformat(),
                "seconds_remaining": remaining,
                "safety_margin_s": safety_margin_s,
            },
        )


def auth_header(token: str) -> Dict[str, str]:
    """The ``Authorization`` header the SPA's interceptor sends on every request.

    Surrounding whitespace is stripped because a pasted token usually arrives with a
    trailing newline, which is not a legal header value.
    """
    return {"Authorization": "Bearer " + token.strip()}


__all__ = [
    "CLAIM_PREFIX",
    "BARE_CLAIM_NAMES",
    "TokenInfo",
    "auth_header",
    "claim",
    "decode_jwt_claims",
    "inspect_token",
    "preflight_token",
]
