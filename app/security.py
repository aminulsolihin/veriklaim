"""Password hashing, card-number tokens and a small sign-in rate limiter."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from .config import settings

_N, _R, _P = 2 ** 14, 8, 1  # scrypt cost; about 16 MB and a few tens of milliseconds


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


# Compared against when the username does not exist, so both cases take the same time.
DUMMY_HASH = hash_password(secrets.token_hex(8))

_card_key = hashlib.sha256(("card-token:" + settings.secret_key).encode("utf-8")).digest()


def card_token(nokartu: str) -> str:
    """One-way token for a card number. The same card gives the same token, so repeat
    top-ups can be counted, but the number cannot be recovered without the server key."""
    value = (nokartu or "").strip()
    if not value:
        return ""
    mac = hmac.new(_card_key, value.encode("utf-8"), hashlib.sha256).digest()
    return "P-" + base64.b32encode(mac[:5]).decode().rstrip("=")


def client_ip(request) -> str:
    """The caller's address for rate limits. With PROXY_HOPS = n, the n-th entry from the right of
    X-Forwarded-For (the one our own proxy wrote); entries to its left come from the client and are
    ignored. With 0 hops, or a header too short to trust, the socket address."""
    hops = settings.proxy_hops
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops][:64]
    return request.client.host if request.client else "?"


class LoginLimiter:
    """At most `limit` failed sign-ins per key in `window` seconds. In memory, single process.

    Only keys that have failed are stored, and expired keys are swept, so the table cannot grow
    from keys that never fail."""

    _SWEEP_AT = 2048

    def __init__(self, limit: int = 8, window: int = 600) -> None:
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: float):
        q = self._fails.get(key)
        if q is None:
            return None
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._fails[key]
            return None
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            q = self._live(key, time.monotonic())
            return q is not None and len(q) >= self.limit

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._fails) >= self._SWEEP_AT:
                for k in list(self._fails):
                    self._live(k, now)
            self._fails.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


login_limiter = LoginLimiter()
