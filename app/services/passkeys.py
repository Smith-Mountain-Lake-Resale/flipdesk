"""Passkeys (WebAuthn) for local accounts.

* Sign in with a passkey alone (discoverable credential + user verification) — counts as
  two-factor, so no TOTP step follows.
* Or use a passkey as the second step after a password, instead of an authenticator code.
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import urlparse

from fastapi import Request
from sqlalchemy.orm import Session
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from .. import config, settings
from ..models import Passkey, User
from ..util import utcnow
from . import authn

CHALLENGE_TTL = 300


class PasskeyError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.get("PASSKEYS_ENABLED"))


def allowed_for(user: User | None) -> bool:
    """Passkeys are for local accounts; SSO accounts authenticate at the identity provider."""
    return bool(user) and enabled() and user.auth_source != "oidc"


def _origin(request: Request) -> str:
    base = (settings.get("PUBLIC_URL") or "").rstrip("/")
    if base:
        u = urlparse(base)
        return f"{u.scheme}://{u.netloc}"
    return str(request.base_url).rstrip("/")


def rp(request: Request) -> tuple[str, str]:
    """(rp_id, expected_origin). rp_id is the bare host name of the public URL."""
    origin = _origin(request)
    return urlparse(origin).hostname or "localhost", origin


def _descriptors(creds: list[Passkey]) -> list[PublicKeyCredentialDescriptor]:
    out = []
    for c in creds:
        transports = []
        for t in (c.transports or "").split(","):
            try:
                transports.append(AuthenticatorTransport(t))
            except ValueError:
                pass
        out.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id), transports=transports or None))
    return out


def _stash(request: Request, key: str, challenge: bytes, **extra) -> None:
    request.session[key] = {"c": bytes_to_base64url(challenge), "t": int(time.time()), **extra}


def _pop(request: Request, key: str) -> dict:
    data = request.session.pop(key, None)
    if not data or time.time() - data.get("t", 0) > CHALLENGE_TTL:
        raise PasskeyError("The passkey request expired. Please try again.")
    return data


# ------------------------------------------------------------------ registration
def registration_options(request: Request, db: Session, user: User) -> str:
    if not allowed_for(user):
        raise PasskeyError("Passkeys aren't available for this account.")
    if not user.webauthn_user_id:
        user.webauthn_user_id = bytes_to_base64url(secrets.token_bytes(32))
        db.commit()
    rp_id, _ = rp(request)
    existing = db.query(Passkey).filter_by(user_id=user.id).all()
    challenge = secrets.token_bytes(32)
    opts = generate_registration_options(
        rp_id=rp_id, rp_name=config.APP_NAME, user_name=user.username,
        user_id=base64url_to_bytes(user.webauthn_user_id),
        user_display_name=user.display_name or user.username, challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=_descriptors(existing))
    _stash(request, "pk_reg", challenge, uid=user.id)
    return options_to_json(opts)


def register(request: Request, db: Session, user: User, credential: dict, name: str) -> tuple[Passkey, list[str] | None]:
    """Verify and store a new passkey. Returns (passkey, fresh recovery codes or None)."""
    st = _pop(request, "pk_reg")
    if st.get("uid") != user.id:
        raise PasskeyError("Passkey request didn't match your session.")
    rp_id, origin = rp(request)
    try:
        v = verify_registration_response(credential=credential, expected_challenge=base64url_to_bytes(st["c"]),
                                         expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True)
    except Exception as exc:
        raise PasskeyError(f"Your device's response couldn't be verified: {exc}")
    cred_id = bytes_to_base64url(v.credential_id)
    if db.query(Passkey.id).filter_by(credential_id=cred_id).first():
        raise PasskeyError("That passkey is already registered.")
    transports = ",".join((credential.get("response") or {}).get("transports") or [])
    pk = Passkey(user_id=user.id, credential_id=cred_id, public_key=bytes_to_base64url(v.credential_public_key),
                 sign_count=v.sign_count, transports=transports or None, aaguid=str(v.aaguid),
                 backed_up=bool(v.credential_backed_up), device_type=str(getattr(v.credential_device_type, "value", v.credential_device_type)),
                 name=(name or "").strip()[:100] or default_name(request, v.credential_backed_up))
    db.add(pk)
    user.passkey_count = (user.passkey_count or 0) + 1
    codes = None
    if authn.recovery_codes_left(user) == 0:
        codes = authn.new_recovery_codes(user)  # first second factor → give recovery codes
    db.commit()
    return pk, codes


def default_name(request: Request, synced: bool) -> str:
    ua = request.headers.get("user-agent", "")
    for key, label in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
                       ("Windows", "Windows"), ("CrOS", "Chromebook"), ("Linux", "Linux")):
        if key in ua:
            return f"{label} passkey"
    return "Synced passkey" if synced else "Passkey"


def delete(db: Session, user: User, pk_id: int) -> None:
    pk = db.query(Passkey).filter_by(id=pk_id, user_id=user.id).first()
    if not pk:
        raise PasskeyError("Passkey not found.")
    db.delete(pk)
    user.passkey_count = max((user.passkey_count or 1) - 1, 0)
    if user.passkey_count == 0 and not user.totp_enabled:
        user.recovery_codes = None
    db.commit()


def remove_all(db: Session, user: User) -> int:
    n = db.query(Passkey).filter_by(user_id=user.id).delete()
    user.passkey_count = 0
    if not user.totp_enabled:
        user.recovery_codes = None
    db.commit()
    return n


# ------------------------------------------------------------------ authentication
def authentication_options(request: Request, db: Session, for_user: User | None = None) -> str:
    """Options for a passkey sign-in. With `for_user` (second step) only their passkeys are allowed;
    otherwise the browser offers any discoverable passkey for this site."""
    rp_id, _ = rp(request)
    challenge = secrets.token_bytes(32)
    allow = _descriptors(db.query(Passkey).filter_by(user_id=for_user.id).all()) if for_user else []
    opts = generate_authentication_options(rp_id=rp_id, challenge=challenge, allow_credentials=allow or None,
                                           user_verification=UserVerificationRequirement.REQUIRED if not for_user
                                           else UserVerificationRequirement.PREFERRED)
    _stash(request, "pk_auth", challenge, uid=for_user.id if for_user else None)
    return options_to_json(opts)


def authenticate(request: Request, db: Session, credential: dict) -> tuple[User, bool]:
    """Verify an assertion. Returns (user, user_verified)."""
    st = _pop(request, "pk_auth")
    cred_id = credential.get("id") or credential.get("rawId") or ""
    pk = db.query(Passkey).filter_by(credential_id=cred_id).first()
    if not pk:
        raise PasskeyError("This passkey isn't registered with FlipDesk (it may have been removed).")
    user = db.get(User, pk.user_id)
    if st.get("uid") and st["uid"] != user.id:
        raise PasskeyError("That passkey belongs to a different account.")
    if not allowed_for(user) or user.is_active is False:
        raise PasskeyError("Passkey sign-in isn't available for this account.")
    # user handle (when present) must match the account
    handle = (credential.get("response") or {}).get("userHandle")
    if handle and user.webauthn_user_id and handle != user.webauthn_user_id:
        raise PasskeyError("Passkey user handle mismatch.")
    rp_id, origin = rp(request)
    primary = not st.get("uid")
    try:
        v = verify_authentication_response(
            credential=credential, expected_challenge=base64url_to_bytes(st["c"]), expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=base64url_to_bytes(pk.public_key),
            credential_current_sign_count=pk.sign_count or 0, require_user_verification=primary)
    except Exception as exc:
        raise PasskeyError(f"Passkey couldn't be verified: {exc}")
    pk.sign_count = v.new_sign_count
    pk.backed_up = bool(v.credential_backed_up)
    pk.last_used_at = utcnow()
    db.commit()
    return user, bool(v.user_verified)


def list_for(db: Session, user: User) -> list[Passkey]:
    return db.query(Passkey).filter_by(user_id=user.id).order_by(Passkey.created_at).all()


def parse_body(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise PasskeyError("Bad request.")
    if not isinstance(data, dict):
        raise PasskeyError("Bad request.")
    return data
