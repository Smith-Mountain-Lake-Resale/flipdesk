from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import config, settings
from ..db import get_db
from ..models import AutomationLog, JobRun, Listing, Notification, User
from ..services import automations, issues, notify, scheduler
from ..util import hash_password, utcnow, verify_password
from ..web import current_user, form, redirect, render

router = APIRouter()

GROUPS = {"sync": "Sync & reconciliation", "alerts": "Alerts", "tax": "Tax & money", "system": "Backups"}


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
    settings.set_many(db, _save_group(f, group))
    scheduler.reschedule()
    back = "/automations" if group == "automation" else f"/settings?tab={group}"
    return redirect(back, request, "Settings saved.")


@router.post("/settings-test-alerts")
async def test_alerts(request: Request):
    await form(request)
    res = notify.test_all()
    return redirect("/settings?tab=alerts", request, " · ".join(res), "ok" if all("ok" in r for r in res) else "err")


@router.post("/settings-users/add")
async def user_add(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    u = (f.get("username") or "").strip()
    pw = f.get("password") or ""
    if len(u) < 2 or len(pw) < 8:
        return redirect("/settings?tab=users", request, "Username ≥ 2 and password ≥ 8 characters.", "err")
    if db.query(User).filter_by(username=u).first():
        return redirect("/settings?tab=users", request, "That username exists.", "err")
    db.add(User(username=u, password_hash=hash_password(pw)))
    db.commit()
    return redirect("/settings?tab=users", request, f"User {u} added.")


@router.post("/settings-users/password")
async def user_password(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    u = db.query(User).filter_by(username=current_user(request)).first()
    if not u or not verify_password(f.get("current") or "", u.password_hash):
        return redirect("/settings?tab=users", request, "Current password is wrong.", "err")
    if len(f.get("new") or "") < 8 or f.get("new") != f.get("new2"):
        return redirect("/settings?tab=users", request, "New passwords must match and be ≥ 8 characters.", "err")
    u.password_hash = hash_password(f.get("new"))
    db.commit()
    return redirect("/settings?tab=users", request, "Password changed.")


@router.post("/settings-users/{uid}/delete")
async def user_delete(uid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    if db.query(User).count() <= 1:
        return redirect("/settings?tab=users", request, "Can't delete the last user.", "err")
    u = db.get(User, uid)
    if u and u.username != current_user(request):
        db.delete(u)
        db.commit()
    return redirect("/settings?tab=users", request, "User removed.")


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
