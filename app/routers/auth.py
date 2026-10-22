from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import settings
from ..db import get_db
from ..models import User
from ..services import authn, oidc, passkeys
from ..util import hash_password, verify_password
from ..web import flash, form, redirect, render

router = APIRouter()

# A real hash so unknown usernames take as long as wrong passwords.
_DUMMY_HASH = hash_password("not-a-real-password")


def _safe_next(nxt: str | None) -> str:
    nxt = nxt or "/"
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/\\") else "/"


def _public_base(request: Request) -> str:
    base = (settings.get("PUBLIC_URL") or "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


def _login_ctx(request: Request, **extra) -> dict:
    return {"oidc": oidc.enabled(), "oidc_name": settings.get("OIDC_DISPLAY_NAME") or "SSO",
            "passkeys": passkeys.enabled(),
            "password_login": not (oidc.enabled() and settings.get("OIDC_DISABLE_PASSWORD_LOGIN")), **extra}


# ------------------------------------------------------------------ first run
@router.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    if db.query(User.id).first():
        return redirect("/login")
    return render(request, "setup.html", _login_ctx(request))


@router.post("/setup")
async def setup_submit(request: Request, db: Session = Depends(get_db)):
    if db.query(User.id).first():
        return redirect("/login")
    f = await form(request)
    username = (f.get("username") or "").strip()
    pw = f.get("password") or ""
    if len(username) < 2 or len(pw) < 8 or pw != f.get("password2"):
        return render(request, "setup.html", _login_ctx(request, error="Username ≥ 2 characters, password ≥ 8 characters, and both passwords must match.", username=username))
    user = User(username=username, password_hash=hash_password(pw), is_admin=True, auth_source="local",
                email=(f.get("email") or "").strip().lower() or None)
    db.add(user)
    db.commit()
    nxt = authn.begin_login(request, db, user, "password")
    flash(request, "Welcome! Next, connect your eBay developer keys.")
    return redirect(nxt or "/settings/ebay")


# ------------------------------------------------------------------ password login
@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if not db.query(User.id).first():
        return redirect("/setup")
    ctx = _login_ctx(request, next=_safe_next(request.query_params.get("next")))
    if ctx["oidc"] and settings.get("OIDC_AUTO_REDIRECT") and not request.query_params.get("local") \
            and not request.query_params.get("error"):
        return redirect(f"/auth/oidc/login?next={ctx['next']}")
    if request.query_params.get("error"):
        ctx["error"] = request.query_params.get("error")[:300]
    return render(request, "login.html", ctx)


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    nxt = _safe_next(f.get("next"))
    ctx = _login_ctx(request, next=nxt)
    if not ctx["password_login"]:
        return render(request, "login.html", {**ctx, "error": "Password sign-in is disabled. Use single sign-on."}, 403)
    username = (f.get("username") or "").strip()
    ip = authn.client_ip(request)
    keys = (f"ip:{ip}", f"ipu:{ip}:{username.lower()}")
    wait = max(authn.login_throttle.blocked(keys[1]), authn.ip_throttle.blocked(keys[0]))
    if wait:
        return render(request, "login.html", {**ctx, "error": f"Too many failed attempts. Try again in {wait // 60 + 1} minutes."}, 429)
    u = db.query(User).filter(func.lower(User.username) == username.lower()).first()
    ok = verify_password(f.get("password") or "", u.password_hash if (u and u.has_password) else _DUMMY_HASH)
    if not u or not ok or not u.has_password:
        authn.login_throttle.fail(keys[1])
        authn.ip_throttle.fail(keys[0])
        return render(request, "login.html", {**ctx, "error": "Invalid username or password."}, 401)
    if u.is_active is False:
        return render(request, "login.html", {**ctx, "error": "This account is disabled."}, 403)
    authn.login_throttle.reset(keys[1])
    request.session["login_next"] = nxt
    step = authn.begin_login(request, db, u, "password")
    return redirect(step or nxt)


# ------------------------------------------------------------------ TOTP step
def _pending(request: Request, db: Session) -> User | None:
    uid = request.session.get("pending_uid")
    if not uid or time.time() - request.session.get("pending_at", 0) > 600:
        return None
    return db.get(User, uid)


def _mfa_ctx(u: User) -> dict:
    return {"has_totp": bool(u.totp_enabled), "has_passkey": passkeys.enabled() and (u.passkey_count or 0) > 0}


@router.get("/login/mfa")
def mfa_page(request: Request, db: Session = Depends(get_db)):
    u = _pending(request, db)
    if not u:
        return redirect("/login", request, "Your sign-in timed out. Please sign in again.", "err")
    return render(request, "login_mfa.html", {"recovery": request.query_params.get("recovery") == "1",
                                              "left": authn.recovery_codes_left(u), **_mfa_ctx(u)})


@router.post("/login/mfa")
async def mfa_submit(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    u = _pending(request, db)
    if not u:
        return redirect("/login", request, "Your sign-in timed out. Please sign in again.", "err")
    key = f"mfa:{u.id}"
    wait = authn.mfa_throttle.blocked(key)
    recovery = f.get("mode") == "recovery"
    ctx = {"recovery": recovery, "left": authn.recovery_codes_left(u), **_mfa_ctx(u)}
    if wait:
        return render(request, "login_mfa.html", {**ctx, "error": f"Too many wrong codes. Wait {wait} seconds."}, 429)
    code = (f.get("code") or "").strip()
    ok = authn.use_recovery_code(db, u, code) if recovery else authn.verify_totp(db, u, code)
    if not ok:
        authn.mfa_throttle.fail(key)
        return render(request, "login_mfa.html", {**ctx, "error": "That code didn't work." + ("" if recovery else " Check your device's clock, and don't reuse a code you've already used.")}, 401)
    authn.mfa_throttle.reset(key)
    method = request.session.get("pending_method", "password")
    nxt = _safe_next(request.session.get("login_next"))
    authn.finish_login(request, db, u, method)
    if recovery:
        left = authn.recovery_codes_left(u)
        flash(request, f"Signed in with a recovery code. {left} left — consider generating new ones.", "err" if left <= 3 else "ok")
        return redirect("/account")
    return redirect(nxt)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login?local=1")


# ------------------------------------------------------------------ OIDC
@router.get("/auth/oidc/login")
def oidc_login(request: Request):
    if not oidc.enabled():
        return redirect("/login", request, "Single sign-on isn't configured.", "err")
    try:
        url, saved = oidc.start(_public_base(request))
    except Exception as exc:
        return redirect("/login?local=1", request, f"Single sign-on is unavailable: {exc}", "err")
    request.session["oidc"] = saved
    request.session["login_next"] = _safe_next(request.query_params.get("next"))
    return RedirectResponse(url, status_code=303)


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, db: Session = Depends(get_db)):
    q = request.query_params
    saved = request.session.pop("oidc", None)
    if q.get("error"):
        return redirect("/login?local=1", request, f"Sign-in was cancelled or refused: {q.get('error_description') or q.get('error')}", "err")
    if not saved or not q.get("state") or q.get("state") != saved.get("state") or time.time() - saved.get("ts", 0) > 600:
        return redirect("/login?local=1", request, "Sign-in session expired or didn't match. Please try again.", "err")
    try:
        claims = oidc.finish(q.get("code") or "", saved)
        user = oidc.provision(db, claims)
    except oidc.OIDCError as exc:
        return redirect("/login?local=1", request, str(exc), "err")
    except Exception as exc:
        return redirect("/login?local=1", request, f"Single sign-on failed: {exc}", "err")
    nxt = _safe_next(request.session.get("login_next"))
    request.session["login_next"] = nxt
    step = authn.begin_login(request, db, user, "oidc")
    if getattr(user, "_just_created", False):
        flash(request, f"Welcome, {user.display_name or user.username}! Your FlipDesk account was created from {settings.get('OIDC_DISPLAY_NAME') or 'SSO'}.")
    return redirect(step or nxt)


# ------------------------------------------------------------------ passkeys
def _json_csrf(request: Request) -> bool:
    tok = request.headers.get("x-csrf-token")
    return bool(tok) and tok == request.session.get("csrf")


@router.post("/auth/passkey/options")
async def passkey_options(request: Request, db: Session = Depends(get_db)):
    if not _json_csrf(request):
        return JSONResponse({"error": "Page expired — refresh and try again."}, 400)
    if not passkeys.enabled():
        return JSONResponse({"error": "Passkeys are turned off."}, 403)
    second_step = request.query_params.get("step") == "mfa"
    user = _pending(request, db) if second_step else None
    if second_step and not user:
        return JSONResponse({"error": "Your sign-in timed out. Please sign in again."}, 401)
    if not second_step and oidc.enabled() and settings.get("OIDC_DISABLE_PASSWORD_LOGIN"):
        return JSONResponse({"error": "Local sign-in is disabled. Use single sign-on."}, 403)
    if second_step and not (user.passkey_count or 0):
        return JSONResponse({"error": "No passkeys on this account."}, 400)
    return Response(passkeys.authentication_options(request, db, user), media_type="application/json")


@router.post("/auth/passkey/verify")
async def passkey_verify(request: Request, db: Session = Depends(get_db)):
    if not _json_csrf(request):
        return JSONResponse({"error": "Page expired — refresh and try again."}, 400)
    ip = authn.client_ip(request)
    if authn.passkey_throttle.blocked(f"pk:{ip}"):
        return JSONResponse({"error": "Too many failed attempts. Try again in a few minutes."}, 429)
    try:
        body = passkeys.parse_body(await request.body())
        user, _verified = passkeys.authenticate(request, db, body.get("credential") or {})
    except passkeys.PasskeyError as exc:
        authn.passkey_throttle.fail(f"pk:{ip}")
        return JSONResponse({"error": str(exc)}, 400)
    pending = request.session.get("pending_uid")
    if pending:  # second step after a password (or SSO when FlipDesk MFA is required for SSO)
        if pending != user.id:
            return JSONResponse({"error": "That passkey belongs to a different account."}, 400)
        method = request.session.get("pending_method", "password")
        nxt = _safe_next(request.session.get("login_next"))
        authn.finish_login(request, db, user, method)
        return JSONResponse({"redirect": nxt})
    # passkey as the only step (discoverable credential, user verification required)
    if oidc.enabled() and settings.get("OIDC_DISABLE_PASSWORD_LOGIN"):
        return JSONResponse({"error": "Local sign-in is disabled. Use single sign-on."}, 403)
    request.session["login_next"] = _safe_next(body.get("next"))
    nxt = request.session["login_next"]
    step = authn.begin_login(request, db, user, "passkey")
    return JSONResponse({"redirect": step or nxt})
