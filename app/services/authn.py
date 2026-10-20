"""Authentication helpers: sessions, TOTP, recovery codes and brute-force throttling."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import threading
import time

import pyotp
import qrcode
import qrcode.image.svg
from fastapi import Request
from sqlalchemy.orm import Session

from .. import settings
from ..models import User
from ..util import decrypt, encrypt, utcnow


# ------------------------------------------------------------------ sessions
def start_session(request: Request, user: User, method: str) -> None:
    """Fully sign a user in (after password/SSO and, where needed, TOTP)."""
    request.session.pop("pending_uid", None)
    request.session.pop("pending_method", None)
    request.session.pop("pending_totp_secret", None)
    request.session["uid"] = user.id
    request.session["user"] = user.username
    request.session["epoch"] = user.session_epoch or 0
    request.session["method"] = method
    request.session["mfa_ok"] = bool(user.totp_enabled)
    request.session["csrf"] = secrets.token_urlsafe(24)  # rotate on privilege change


def begin_login(request: Request, db: Session, user: User, method: str) -> str:
    """First factor passed. Returns where to send the browser next."""
    nxt = request.session.get("login_next")
    request.session.clear()
    if nxt:
        request.session["login_next"] = nxt
    if mfa_needed_for(user, method):
        request.session["pending_uid"] = user.id
        request.session["pending_method"] = method
        request.session["pending_at"] = int(time.time())
        return "/login/mfa"
    finish_login(request, db, user, method)
    if must_enroll(user, method):
        return "/account/mfa?required=1"
    return ""


def finish_login(request: Request, db: Session, user: User, method: str) -> None:
    user.last_login = utcnow()
    db.commit()
    start_session(request, user, method)


def mfa_needed_for(user: User, method: str) -> bool:
    if not user.totp_enabled:
        return False
    if method == "oidc" and not settings.get("MFA_REQUIRED_FOR_OIDC"):
        return False  # IdP handles MFA
    return True


def must_enroll(user: User, method: str) -> bool:
    """Is this user blocked until they set up TOTP?"""
    if user.totp_enabled or not settings.get("MFA_REQUIRED"):
        return False
    if method == "oidc" and not settings.get("MFA_REQUIRED_FOR_OIDC"):
        return False
    return True


def sign_out_everywhere(user: User) -> None:
    user.session_epoch = (user.session_epoch or 0) + 1


# ------------------------------------------------------------------ TOTP
def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(user: User, secret: str) -> str:
    issuer = settings.get("MFA_ISSUER_NAME") or "FlipDesk"
    return pyotp.TOTP(secret).provisioning_uri(name=user.email or user.username, issuer_name=issuer)


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.find("<svg"):]


def _matching_counter(secret: str, code: str, window: int = 1) -> int | None:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now = time.time()
    for offset in range(-window, window + 1):
        t = now + offset * totp.interval
        if hmac.compare_digest(totp.at(t), code):
            return int(t // totp.interval)
    return None


def verify_code_for_secret(secret: str, code: str) -> int | None:
    return _matching_counter(secret, code)


def verify_totp(db: Session, user: User, code: str) -> bool:
    secret = decrypt(user.totp_secret)
    if not secret:
        return False
    counter = _matching_counter(secret, code)
    if counter is None or counter <= (user.totp_last_counter or 0):
        return False  # wrong, or a replay of an already-used code
    user.totp_last_counter = counter
    db.commit()
    return True


def enable_totp(db: Session, user: User, secret: str, counter: int) -> list[str]:
    user.totp_secret = encrypt(secret)
    user.totp_enabled = True
    user.totp_enabled_at = utcnow()
    user.totp_last_counter = counter
    codes = new_recovery_codes(user)
    db.commit()
    return codes


def disable_totp(db: Session, user: User) -> None:
    user.totp_secret = None
    user.totp_enabled = False
    user.totp_enabled_at = None
    user.totp_last_counter = 0
    user.recovery_codes = None
    db.commit()


# ------------------------------------------------------------------ recovery codes
def _hash_code(code: str) -> str:
    norm = "".join(ch for ch in code.lower() if ch.isalnum())
    return hashlib.sha256(("flipdesk-rc:" + norm).encode()).hexdigest()


def new_recovery_codes(user: User, n: int = 10) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
             for _ in range(n)]
    user.recovery_codes = json.dumps([_hash_code(c) for c in codes])
    return codes


def recovery_codes_left(user: User) -> int:
    try:
        return len(json.loads(user.recovery_codes or "[]"))
    except ValueError:
        return 0


def use_recovery_code(db: Session, user: User, code: str) -> bool:
    try:
        hashes = json.loads(user.recovery_codes or "[]")
    except ValueError:
        return False
    h = _hash_code(code)
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            hashes.remove(stored)
            user.recovery_codes = json.dumps(hashes)
            db.commit()
            return True
    return False


# ------------------------------------------------------------------ throttling
class Throttle:
    """In-memory sliding-window limiter (single app process by design)."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> int:
        """Seconds until unblocked, or 0."""
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            self._hits[key] = hits
            if len(hits) >= self.limit:
                return int(self.window - (now - hits[0])) + 1
        return 0

    def fail(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_throttle = Throttle(limit=10, window=15 * 60)   # per IP + username
ip_throttle = Throttle(limit=50, window=15 * 60)      # per IP (password spraying)
mfa_throttle = Throttle(limit=5, window=5 * 60)       # per user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
