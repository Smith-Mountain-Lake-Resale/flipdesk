"""Shared web helpers: templates, auth, CSRF, flash messages."""
from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, settings
from .util import money

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _money(v):
    if v is None:
        return "—"
    sym = settings.get("CURRENCY_SYMBOL") or "$"
    return money(v).replace("$", sym)


def _pct(v):
    return "—" if v is None else f"{v:,.1f}%"


def _dt(v, fmt="%Y-%m-%d"):
    if not v:
        return "—"
    try:
        return v.strftime(fmt)
    except AttributeError:
        return str(v)


templates.env.filters["money"] = _money
templates.env.filters["pct"] = _pct
templates.env.filters["dt"] = _dt
templates.env.globals["APP_NAME"] = config.APP_NAME
templates.env.globals["APP_VERSION"] = config.APP_VERSION


PUBLIC_PATHS = ("/login", "/setup", "/static/", "/health", "/ebay/account-deletion", "/privacy", "/favicon",
                "/auth/oidc/", "/manifest.webmanifest", "/sw.js", "/offline", "/apple-touch-icon")

# Paths a signed-in user may visit while they're required to set up MFA.
ENROLL_PATHS = ("/account/mfa", "/logout", "/static/")
templates.env.globals["V"] = config.APP_VERSION

# Admin-only areas (configuration, data, users).
ADMIN_PATHS = ("/settings", "/data", "/sync/", "/ebay/connect", "/ebay/manual-code", "/jobs")


def is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PATHS)


def current_user(request: Request) -> str | None:
    if config.AUTH_MODE == "none":
        return "admin"
    u = getattr(request.state, "user", None)
    if u is not None:
        return u.display_name or u.username
    return None


def current_user_obj(request: Request):
    return getattr(request.state, "user", None)


def is_admin(request: Request) -> bool:
    if config.AUTH_MODE == "none":
        return True
    u = current_user_obj(request)
    return bool(u and u.is_admin)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def form(request: Request) -> dict:
    """Parse a POSTed form and enforce the CSRF token."""
    data = await request.form()
    if data.get("_csrf") != request.session.get("csrf"):
        raise HTTPException(400, "Form expired or invalid (CSRF). Go back, refresh and try again.")
    return data


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    ctx = dict(ctx or {})
    msgs = request.session.pop("flash", []) if "session" in request.scope else []
    ctx.update(request=request, user=current_user(request), csrf=csrf_token(request), flashes=msgs,
               path=request.url.path, unread=getattr(request.state, "unread", 0),
               auth_mode=config.AUTH_MODE, me=current_user_obj(request), admin=is_admin(request),
               enrolling=bool(getattr(request.state, "must_enroll", False)))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, request: Request | None = None, msg: str | None = None, kind: str = "ok"):
    if request is not None and msg:
        flash(request, msg, kind)
    return RedirectResponse(url, status_code=303)


def login_redirect(request: Request):
    nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
