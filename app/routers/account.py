"""Self-service account page: password, TOTP enrollment, recovery codes, sessions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import settings
from ..db import get_db
from ..models import User
from ..services import authn
from ..util import hash_password, verify_password
from ..web import current_user_obj, form, redirect, render

router = APIRouter()


def _me(request: Request, db: Session) -> User | None:
    u = current_user_obj(request)
    return db.get(User, u.id) if u else None


def _ctx(request: Request, me: User) -> dict:
    method = request.session.get("method", "password")
    return {"u": me, "left": authn.recovery_codes_left(me), "required": settings.get("MFA_REQUIRED"),
            "must_enroll": authn.must_enroll(me, method), "method": method,
            "oidc_trusted": not settings.get("MFA_REQUIRED_FOR_OIDC")}


@router.get("/account")
def account(request: Request, db: Session = Depends(get_db)):
    me = _me(request, db)
    if me is None:
        return redirect("/settings")  # AUTH_MODE=none
    return render(request, "account.html", _ctx(request, me))


@router.post("/account/profile")
async def account_profile(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = _me(request, db)
    if me.auth_source != "oidc":
        email = (f.get("email") or "").strip().lower() or None
        if email and db.query(User.id).filter(func.lower(User.email) == email, User.id != me.id).first():
            return redirect("/account", request, "Another user already has that email.", "err")
        me.email = email
    me.display_name = (f.get("display_name") or "").strip() or None
    db.commit()
    return redirect("/account", request, "Profile saved.")


@router.post("/account/password")
async def account_password(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = _me(request, db)
    if me.has_password and not verify_password(f.get("current") or "", me.password_hash):
        return redirect("/account", request, "Current password is wrong.", "err")
    new = f.get("new") or ""
    if len(new) < 8 or new != f.get("new2"):
        return redirect("/account", request, "New passwords must match and be at least 8 characters.", "err")
    me.password_hash = hash_password(new)
    authn.sign_out_everywhere(me)
    db.commit()
    authn.start_session(request, me, request.session.get("method", "password"))
    return redirect("/account", request, "Password changed. Other sessions were signed out.")


@router.post("/account/sign-out-everywhere")
async def sign_out_all(request: Request, db: Session = Depends(get_db)):
    await form(request)
    me = _me(request, db)
    authn.sign_out_everywhere(me)
    db.commit()
    authn.start_session(request, me, request.session.get("method", "password"))
    return redirect("/account", request, "All other sessions were signed out.")


# ------------------------------------------------------------------ TOTP enrollment
@router.get("/account/mfa")
def mfa_setup(request: Request, db: Session = Depends(get_db)):
    me = _me(request, db)
    if me is None:
        return redirect("/")
    if me.totp_enabled:
        return redirect("/account")
    secret = request.session.get("pending_totp_secret")
    if not secret:
        secret = authn.new_secret()
        request.session["pending_totp_secret"] = secret
    uri = authn.provisioning_uri(me, secret)
    return render(request, "mfa_setup.html", {**_ctx(request, me), "secret": secret, "uri": uri,
                                              "qr": authn.qr_svg(uri)})


@router.post("/account/mfa")
async def mfa_enable(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = _me(request, db)
    secret = request.session.get("pending_totp_secret")
    if not secret:
        return redirect("/account/mfa", request, "Setup expired — scan the new QR code.", "err")
    key = f"enroll:{me.id}"
    if authn.mfa_throttle.blocked(key):
        return redirect("/account/mfa", request, "Too many wrong codes. Wait a few minutes.", "err")
    counter = authn.verify_code_for_secret(secret, f.get("code") or "")
    if counter is None:
        authn.mfa_throttle.fail(key)
        return redirect("/account/mfa", request, "That code didn't match. Make sure your phone's clock is set automatically and try the current code.", "err")
    codes = authn.enable_totp(db, me, secret, counter)
    request.session.pop("pending_totp_secret", None)
    request.session["mfa_ok"] = True
    return render(request, "recovery_codes.html", {"codes": codes, "fresh": True})


@router.post("/account/mfa/disable")
async def mfa_disable(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = _me(request, db)
    if not me.totp_enabled:
        return redirect("/account")
    code = f.get("code") or ""
    if not (authn.verify_totp(db, me, code) or authn.use_recovery_code(db, me, code)):
        return redirect("/account", request, "Enter a current authenticator code (or a recovery code) to confirm.", "err")
    authn.disable_totp(db, me)
    if settings.get("MFA_REQUIRED"):
        return redirect("/account/mfa?required=1", request, "Authenticator removed. Two-factor is required here, so set up your new one now.")
    return redirect("/account", request, "Two-factor authentication turned off.")


@router.post("/account/mfa/recovery")
async def mfa_recovery(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = _me(request, db)
    if not me.totp_enabled or not authn.verify_totp(db, me, f.get("code") or ""):
        return redirect("/account", request, "Enter a current authenticator code to generate new recovery codes.", "err")
    codes = authn.new_recovery_codes(me)
    db.commit()
    return render(request, "recovery_codes.html", {"codes": codes, "fresh": False})
