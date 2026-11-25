"""Login: one hard-coded user, sessions in memory, CSRF tokens, a login throttle.

The user and a PBKDF2 hash of the password live in a JSON file on the server
(``secrets/webapp_users.json`` by default), never in the repository. ``make-user`` on the
command line writes it. A session is a random cookie value held in memory: restarting the
service logs everyone out, which is the intended behaviour for a tool this sensitive.

Every POST must carry the session's CSRF token. The cookie is ``Secure``, ``HttpOnly``
and ``SameSite=Strict``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

PBKDF2_ITERATIONS = 240000
SESSION_TTL_S = 12 * 3600
COOKIE_NAME = "secop_session"

# At most this many failed logins per address per window; then every attempt is refused
# until the window passes, right password or not.
LOGIN_FAILURES_ALLOWED = 5
LOGIN_FAILURE_WINDOW_S = 300


def hash_password(password: str, salt: Optional[bytes] = None) -> Dict[str, object]:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {
        "salt": salt.hex(),
        "hash": digest.hex(),
        "iterations": PBKDF2_ITERATIONS,
        "algorithm": "pbkdf2_sha256",
    }


def write_user_file(path: str, username: str, password: str) -> None:
    record = {"username": username}
    record.update(hash_password(password))
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)


@dataclass(frozen=True)
class UserRecord:
    username: str
    salt: bytes
    digest: bytes
    iterations: int

    def check(self, username: str, password: str) -> bool:
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), self.salt, self.iterations
        )
        # Both comparisons always run, so the time taken does not say which one failed.
        user_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        pass_ok = hmac.compare_digest(candidate, self.digest)
        return user_ok and pass_ok


def load_user_file(path: str) -> UserRecord:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return UserRecord(
        username=str(data["username"]),
        salt=bytes.fromhex(data["salt"]),
        digest=bytes.fromhex(data["hash"]),
        iterations=int(data["iterations"]),
    )


@dataclass
class Session:
    token: str
    csrf: str
    username: str
    expires_at: float


class Sessions:
    """Thread-safe session store and login throttle."""

    def __init__(self, ttl_s: float = SESSION_TTL_S, clock=time.time) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}  # type: Dict[str, Session]
        self._failures = {}  # type: Dict[str, List[float]]

    def create(self, username: str) -> Session:
        session = Session(
            token=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(24),
            username=username,
            expires_at=self._clock() + self._ttl_s,
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get(self, token: Optional[str]) -> Optional[Session]:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at < self._clock():
                del self._sessions[token]
                return None
            return session

    def drop(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def throttled(self, address: str) -> bool:
        now = self._clock()
        with self._lock:
            recent = [t for t in self._failures.get(address, []) if now - t < LOGIN_FAILURE_WINDOW_S]
            self._failures[address] = recent
            return len(recent) >= LOGIN_FAILURES_ALLOWED

    def record_failure(self, address: str) -> None:
        with self._lock:
            self._failures.setdefault(address, []).append(self._clock())

    def clear_failures(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


def parse_cookies(header: Optional[str]) -> Dict[str, str]:
    out = {}  # type: Dict[str, str]
    for part in (header or "").split(";"):
        name, _sep, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def session_cookie(session: Session, secure: bool) -> str:
    attrs = ["%s=%s" % (COOKIE_NAME, session.token), "Path=/", "HttpOnly", "SameSite=Strict"]
    attrs.append("Max-Age=%d" % SESSION_TTL_S)
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def clear_cookie(secure: bool) -> str:
    attrs = ["%s=" % COOKIE_NAME, "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def csrf_ok(session: Session, submitted: Optional[str]) -> bool:
    return bool(submitted) and hmac.compare_digest(session.csrf, submitted or "")


def new_password(length: int = 20) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


__all__ = [
    "COOKIE_NAME",
    "Session",
    "Sessions",
    "UserRecord",
    "clear_cookie",
    "csrf_ok",
    "hash_password",
    "load_user_file",
    "new_password",
    "parse_cookies",
    "session_cookie",
    "write_user_file",
]

