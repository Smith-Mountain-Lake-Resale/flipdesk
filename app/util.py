from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from datetime import date, datetime, timezone

from cryptography.fernet import Fernet, InvalidToken


def utcnow() -> datetime:
    """Naive UTC datetime (stored the same way on every DB backend)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    dt = parse_iso(s)
    return dt.date() if dt else None


def to_float(v, default: float | None = 0.0) -> float | None:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if s in ("", "-", ".", "-."):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def money(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


# ---------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---------------------------------------------------------------- encryption
def _fernet() -> Fernet:
    from .config import SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(("flipdesk:" + SECRET_KEY).encode()).digest())
    return Fernet(key)


def encrypt(s: str | None) -> str | None:
    if s is None:
        return None
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        return None
