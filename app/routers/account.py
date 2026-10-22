"""Self-service account page: password, TOTP enrollment, recovery codes, sessions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import settings
from ..db import get_db
from ..models import Passkey, User
from ..services import authn, passkeys
from ..util import hash_password, verify_password
from ..web import current_user_obj, flash, form, redirect, render

router = APIRouter()


def _me(request: Request, db: Session) -> User | None:
    u = current_user_obj(request)
    return db.get(User, u.id) if u else None


def _ctx(request: Request, me: User, db: Session) -> dict:
    method = request.session.get("method", "password")
    return {"u": me, "left": authn.recovery_codes_left(me), "required": settings.get("MFA_REQUIRED"),
            "must_enroll": authn.must_enroll(me, method), "method": method,
            "oidc_trusted": not settings.get("MFA_REQUIRED_FOR_OIDC"),
            "passkeys_ok": passkeys.allowed_for(me), "passkeys": passkeys.list_for(db, me)}


@router.get("/account")
def account(request: Request, db: Session = Depends(get_db)):
    me = _me(request, db)
    if me is None:
        return redirect("/settings")  # AUTH_MODE=none
    return render(request, "account.html", _ctx(request, me, db))


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
    return render(request, "mfa_setup.html", {**_ctx(request, me, db), "secret": secret, "uri": uri,
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
    if settings.get("MFA_REQUIRED") and not me.has_second_factor:
        return redirect("/account/mfa?required=1", request, "Authenticator removed. Two-factor is required here, so set up your new one now.")
    return redirect("/account", request, "Two-factor authentication turned off.")


@router.post("/account/mfa/recovery")
async def mfa_recovery(request: Request, db: Session = Depends(get_db)):
    """New recovery codes. Confirm with an authenticator code, or (passkey-only accounts) the password."""
    f = await form(request)
    me = _me(request, db)
    if not me.has_second_factor:
        return redirect("/account")
    ok = False
    if me.totp_enabled:
        ok = authn.verify_totp(db, me, f.get("code") or "")
    elif me.has_password:
        ok = verify_password(f.get("password") or "", me.password_hash)
    else:
        ok = request.session.get("method") == "passkey"  # signed in with a passkey just now
    if not ok:
        return redirect("/account", request, "Confirm with a current authenticator code (or your password) to generate new recovery codes.", "err")
    codes = authn.new_recovery_codes(me)
    db.commit()
    return render(request, "recovery_codes.html", {"codes": codes, "fresh": False})


# ------------------------------------------------------------------ passkeys
def _json_csrf(request: Request) -> bool:
    tok = request.headers.get("x-csrf-token")
    return bool(tok) and tok == request.session.get("csrf")


@router.post("/account/passkeys/options")
async def passkey_reg_options(request: Request, db: Session = Depends(get_db)):
    if not _json_csrf(request):
        return JSONResponse({"error": "Page expired — refresh and try again."}, 400)
    me = _me(request, db)
    try:
        return Response(passkeys.registration_options(request, db, me), media_type="application/json")
    except passkeys.PasskeyError as exc:
        return JSONResponse({"error": str(exc)}, 400)


@router.post("/account/passkeys/register")
async def passkey_register(request: Request, db: Session = Depends(get_db)):
    if not _json_csrf(request):
        return JSONResponse({"error": "Page expired — refresh and try again."}, 400)
    me = _me(request, db)
    try:
        body = passkeys.parse_body(await request.body())
        pk, codes = passkeys.register(request, db, me, body.get("credential") or {}, body.get("name") or "")
    except passkeys.PasskeyError as exc:
        return JSONResponse({"error": str(exc)}, 400)
    if codes:
        request.session["show_codes"] = codes  # shown once on the next page
        return JSONResponse({"redirect": "/account/recovery-codes"})
    flash(request, f"Passkey “{pk.name}” added.")
    return JSONResponse({"redirect": "/account#passkeys"})


@router.get("/account/recovery-codes")
def show_new_codes(request: Request):
    codes = request.session.pop("show_codes", None)
    if not codes:
        return redirect("/account")
    return render(request, "recovery_codes.html", {"codes": codes, "fresh": True, "passkey": True})


@router.post("/account/passkeys/{pk_id}/rename")
async def passkey_rename(pk_id: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = _me(request, db)
    pk = db.query(Passkey).filter_by(id=pk_id, user_id=me.id).first()
    if pk and (f.get("name") or "").strip():
        pk.name = f.get("name").strip()[:100]
        db.commit()
    return redirect("/account#passkeys", request, "Passkey renamed.")


@router.post("/account/passkeys/{pk_id}/delete")
async def passkey_delete(pk_id: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    me = _me(request, db)
    last_factor = (me.passkey_count or 0) <= 1 and not me.totp_enabled
    if last_factor and settings.get("MFA_REQUIRED") and request.session.get("method") != "oidc":
        return redirect("/account#passkeys", request, "Two-factor is required here — add another passkey or an authenticator app before removing your last passkey.", "err")
    if last_factor and not me.has_password and not me.oidc_sub:
        return redirect("/account#passkeys", request, "This is your only way to sign in. Set a password first.", "err")
    try:
        passkeys.delete(db, me, pk_id)
    except passkeys.PasskeyError as exc:
        return redirect("/account#passkeys", request, str(exc), "err")
    return redirect("/account#passkeys", request, "Passkey removed.")
