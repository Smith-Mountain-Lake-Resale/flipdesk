from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config, settings
from ..db import get_db
from ..models import AutomationLog, JobRun, Listing, Notification, User
from ..services import authn, automations, issues, notify, oidc, scheduler
from ..util import hash_password, utcnow
from ..web import current_user_obj, form, redirect, render

router = APIRouter()

GROUPS = {"sync": "Sync & reconciliation", "alerts": "Alerts", "tax": "Tax & money", "system": "Backups",
          "auth": "Security & MFA", "oidc": "Single sign-on (OIDC)"}


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    tab = request.query_params.get("tab") or "sync"
    if tab not in GROUPS and tab not in ("users", "about"):
        tab = "sync"
    return render(request, "settings.html", {
        "tab": tab, "groups": GROUPS, "fields": settings.group(tab) if tab in GROUPS else [],
        "users": db.query(User).order_by(User.id).all(),
        "about": {"version": config.APP_VERSION, "db": config.redacted_db_url(), "data": str(config.DATA_DIR),
                  "config": str(config.CONFIG_DIR), "config_file": config.CONFIG_FILE.exists(),
                  "auth": config.AUTH_MODE, "tz": config.TIMEZONE},
        "next_runs": scheduler.next_runs(),
        "oidc_redirect": oidc.redirect_uri((settings.get("PUBLIC_URL") or str(request.base_url)).rstrip("/")),
        "recovery": {u.id: authn.recovery_codes_left(u) for u in db.query(User).all()},
        "mfa_required": settings.get("MFA_REQUIRED"),
    })


def _save_group(f, group: str) -> dict:
    vals = {}
    for fld in settings.group(group):
        k = fld["key"]
        if fld["locked"]:
            continue
        if fld["type"] == "bool":
            vals[k] = "true" if f.get(k) == "on" else "false"
        elif k in f:
            v = (f.get(k) or "").strip()
            if fld["secret"] and v == "":
                continue
            if fld["type"] == "json":
                try:
                    json.loads(v)
                except ValueError:
                    raise HTTPException(400, f"{fld['label']} must be valid JSON")
            vals[k] = v
    return vals


@router.post("/settings/{group}")
async def settings_save(group: str, request: Request, db: Session = Depends(get_db)):
    if group not in GROUPS and group != "automation":
        raise HTTPException(404)
    f = await form(request)
    vals = _save_group(f, group)
    note = ""
    if group == "oidc":
        oidc._cache.clear()
        # Don't let an admin lock everyone out before SSO has been proven to work.
        if vals.get("OIDC_DISABLE_PASSWORD_LOGIN") == "true" and not settings.get("OIDC_DISABLE_PASSWORD_LOGIN") \
                and request.session.get("method") != "oidc":
            vals["OIDC_DISABLE_PASSWORD_LOGIN"] = "false"
            note = " Password login was left on — sign in once with SSO first, then you can disable it."
    if group == "auth" and vals.get("MFA_REQUIRED") == "true":
        me = current_user_obj(request)
        if me and not me.totp_enabled:
            note = " You'll be asked to set up your own authenticator now."
    settings.set_many(db, vals)
    scheduler.reschedule()
    if note:
        return redirect(f"/settings?tab={group}", request, "Settings saved." + note, "err" if "left on" in note else "ok")
    back = "/automations" if group == "automation" else f"/settings?tab={group}"
    return redirect(back, request, "Settings saved.")


@router.post("/settings-test-alerts")
async def test_alerts(request: Request):
    await form(request)
    res = notify.test_all()
    return redirect("/settings?tab=alerts", request, " · ".join(res), "ok" if all("ok" in r for r in res) else "err")


def _admins_left(db: Session, excluding: int) -> int:
    return db.query(User).filter(User.is_admin.is_(True), User.is_active.isnot(False), User.id != excluding).count()


@router.post("/settings-users/add")
async def user_add(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    u = (f.get("username") or "").strip()
    pw = f.get("password") or ""
    email = (f.get("email") or "").strip().lower() or None
    sso_only = f.get("sso_only") == "on"
    if len(u) < 2 or (not sso_only and len(pw) < 8):
        return redirect("/settings?tab=users", request, "Username ≥ 2 characters and password ≥ 8 characters (or tick SSO only).", "err")
    if db.query(User).filter(func.lower(User.username) == u.lower()).first():
        return redirect("/settings?tab=users", request, "That username exists.", "err")
    if email and db.query(User).filter(func.lower(User.email) == email).first():
        return redirect("/settings?tab=users", request, "Another user already has that email.", "err")
    db.add(User(username=u, password_hash="!" if sso_only else hash_password(pw), email=email,
                is_admin=f.get("is_admin") == "on", auth_source="local", is_active=True))
    db.commit()
    hint = " They'll be linked on their first SSO sign-in with that email." if (sso_only and email) else ""
    return redirect("/settings?tab=users", request, f"User {u} added.{hint}")


@router.post("/settings-users/{uid}/{action}")
async def user_action(uid: int, action: str, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    me = current_user_obj(request)
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404)
    self_ = me is not None and me.id == u.id
    if action == "delete":
        if self_ or _admins_left(db, u.id) == 0 and u.is_admin:
            return redirect("/settings?tab=users", request, "You can't delete yourself or the last admin.", "err")
        db.delete(u)
        msg = f"Removed {u.username}."
    elif action == "toggle-admin":
        if u.is_admin and (self_ or _admins_left(db, u.id) == 0):
            return redirect("/settings?tab=users", request, "You can't remove your own admin rights or the last admin.", "err")
        u.is_admin = not u.is_admin
        authn.sign_out_everywhere(u)
        msg = f"{u.username} is {'now an admin' if u.is_admin else 'no longer an admin'}."
    elif action == "toggle-active":
        if self_:
            return redirect("/settings?tab=users", request, "You can't disable yourself.", "err")
        if u.is_active is not False and u.is_admin and _admins_left(db, u.id) == 0:
            return redirect("/settings?tab=users", request, "You can't disable the last admin.", "err")
        u.is_active = u.is_active is False
        authn.sign_out_everywhere(u)
        msg = f"{u.username} {'enabled' if u.is_active else 'disabled and signed out'}."
    elif action == "reset-mfa":
        authn.disable_totp(db, u)
        authn.sign_out_everywhere(u)
        msg = f"Two-factor reset for {u.username}." + (" They'll set up a new authenticator at next sign-in." if settings.get("MFA_REQUIRED") else "")
    elif action == "set-password":
        pw = f.get("password") or ""
        if len(pw) < 8:
            return redirect("/settings?tab=users", request, "Password must be at least 8 characters.", "err")
        u.password_hash = hash_password(pw)
        authn.sign_out_everywhere(u)
        msg = f"Password set for {u.username}; their sessions were signed out."
    elif action == "unlink-sso":
        if not u.has_password:
            return redirect("/settings?tab=users", request, "Set a password first, or the user can't sign in.", "err")
        u.oidc_issuer = u.oidc_sub = None
        u.auth_source = "local"
        msg = f"SSO identity unlinked from {u.username}."
    elif action == "sign-out":
        authn.sign_out_everywhere(u)
        msg = f"{u.username} was signed out everywhere."
    else:
        raise HTTPException(404)
    db.commit()
    if self_ and action in ("sign-out", "reset-mfa", "set-password"):
        request.session.clear()
        return redirect("/login?local=1", request, msg)
    return redirect("/settings?tab=users", request, msg)


@router.post("/settings-oidc-test")
async def oidc_test(request: Request):
    await form(request)
    try:
        res = oidc.test_connection()
        return redirect("/settings?tab=oidc", request, " · ".join(res))
    except Exception as exc:
        return redirect("/settings?tab=oidc", request, f"OIDC test failed: {exc}", "err")


# ------------------------------------------------------------------ automations
@router.get("/automations")
def automations_page(request: Request, db: Session = Depends(get_db)):
    campaigns, camp_err = [], None
    if request.query_params.get("campaigns"):
        try:
            campaigns = automations.list_campaigns(db)
        except Exception as exc:
            camp_err = str(exc)
    relist = automations.relist_candidates(db)
    promote = automations.promote_candidates(db)[:50]
    logs = db.query(AutomationLog).order_by(AutomationLog.id.desc()).limit(150).all()
    upcoming = []
    for lst in db.query(Listing).filter(Listing.status == "active", Listing.duration == "GTC",
                                        Listing.end_time <= utcnow() + timedelta(days=2)).order_by(Listing.end_time).limit(30):
        upcoming.append((lst, automations.reprice_target(lst.item, lst)))
    return render(request, "automations.html", {
        "fields": settings.group("automation"), "relist": relist, "promote": promote, "logs": logs,
        "campaigns": campaigns, "camp_err": camp_err, "upcoming": upcoming,
        "dry": settings.get("AUTOMATION_DRY_RUN"), "next_runs": scheduler.next_runs()})


# ------------------------------------------------------------------ issues & notifications
@router.get("/issues")
def issues_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "issues.html", {"issues": issues.detect(db)})


@router.get("/notifications")
def notifications(request: Request, db: Session = Depends(get_db)):
    rows = db.query(Notification).order_by(Notification.id.desc()).limit(200).all()
    return render(request, "notifications.html", {"rows": rows})


@router.post("/notifications/read")
async def notifications_read(request: Request, db: Session = Depends(get_db)):
    await form(request)
    db.query(Notification).filter(Notification.read.is_(False)).update({"read": True})
    db.commit()
    return redirect("/notifications")


@router.get("/jobs")
def jobs(request: Request, db: Session = Depends(get_db)):
    rows = db.query(JobRun).order_by(JobRun.id.desc()).limit(100).all()
    return render(request, "jobs.html", {"rows": rows, "next_runs": scheduler.next_runs()})
