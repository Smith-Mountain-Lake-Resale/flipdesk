from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import settings
from ..db import get_db
from ..models import DeletionNotice, EbayAccount, JobRun, Sale
from ..services import ebay, scheduler
from ..services.sync import counts
from ..web import form, redirect, render

router = APIRouter()


def _public_base(request: Request) -> str:
    base = (settings.get("PUBLIC_URL") or "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


def endpoints(request: Request) -> dict:
    base = _public_base(request)
    return {
        "base": base,
        "accepted": f"{base}/ebay/callback",
        "declined": f"{base}/ebay/declined",
        "privacy": f"{base}/privacy",
        "deletion": f"{base}/ebay/account-deletion",
        "is_https": base.startswith("https://"),
        "configured": bool(settings.get("PUBLIC_URL")),
    }


@router.get("/settings/ebay")
def ebay_settings(request: Request, db: Session = Depends(get_db)):
    acct = db.query(EbayAccount).first()
    jobs = db.query(JobRun).order_by(JobRun.id.desc()).limit(12).all()
    have_keys = all(settings.get(k) for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME"))
    last_deletion = db.query(DeletionNotice).order_by(DeletionNotice.id.desc()).first()
    return render(request, "ebay_setup.html", {
        "acct": acct, "ep": endpoints(request), "fields": settings.group("ebay"), "jobs": jobs,
        "have_keys": have_keys, "counts": counts(db), "scopes": ebay.SCOPES, "next_runs": scheduler.next_runs(),
        "token": settings.get("EBAY_VERIFICATION_TOKEN"), "token_locked": settings.locked_by("EBAY_VERIFICATION_TOKEN"),
        "last_deletion": last_deletion, "env": ebay.env()})


@router.post("/settings/ebay/keys")
async def ebay_keys(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    vals = {}
    for fld in settings.group("ebay"):
        k = fld["key"]
        if fld["locked"]:
            continue
        if fld["type"] == "bool":
            vals[k] = "true" if f.get(k) == "on" else "false"
        elif k in f:
            v = (f.get(k) or "").strip()
            if fld["secret"] and v == "":
                continue  # blank = keep existing secret
            if k == "PUBLIC_URL":
                v = v.rstrip("/")
            vals[k] = v
    tok = vals.get("EBAY_VERIFICATION_TOKEN")
    if tok is not None and tok and not (32 <= len(tok) <= 80 and all(c.isalnum() or c in "_-" for c in tok)):
        return redirect("/settings/ebay", request, "Verification token must be 32–80 characters: letters, digits, _ or -.", "err")
    settings.set_many(db, vals)
    return redirect("/settings/ebay", request, "eBay settings saved.")


@router.post("/settings/ebay/regenerate-token")
async def regen_token(request: Request, db: Session = Depends(get_db)):
    await form(request)
    settings.set_one(db, "EBAY_VERIFICATION_TOKEN", "fd_" + secrets.token_urlsafe(48)[:60].replace("-", "_"))
    return redirect("/settings/ebay#deletion", request, "New verification token generated — update it on eBay too.")


@router.post("/settings/ebay/test")
async def ebay_test(request: Request, db: Session = Depends(get_db)):
    await form(request)
    try:
        ebay._app_token_cache.clear()
        ebay.app_token()
        return redirect("/settings/ebay", request, f"✅ App ID and Cert ID work against eBay {ebay.env()}.")
    except Exception as exc:
        return redirect("/settings/ebay", request, f"Credential test failed: {exc}", "err")


@router.get("/ebay/connect")
def ebay_connect(request: Request):
    try:
        state = ebay.new_state()
        request.session["ebay_state"] = state
        return RedirectResponse(ebay.consent_url(state), status_code=303)
    except ebay.NotConfigured as exc:
        return redirect("/settings/ebay", request, str(exc), "err")


def _finish(request: Request, db: Session, code: str | None, state: str | None, check_state: bool):
    if not code:
        return redirect("/settings/ebay", request, "No authorization code received.", "err")
    if check_state and state != request.session.get("ebay_state"):
        return redirect("/settings/ebay", request, "OAuth state mismatch — start the connection again.", "err")
    try:
        acct = ebay.exchange_code(db, code)
    except Exception as exc:
        return redirect("/settings/ebay", request, f"Could not connect: {exc}", "err")
    request.session.pop("ebay_state", None)
    scheduler.run_async("full_sync")
    who = acct.username or "your account"
    return redirect("/settings/ebay", request,
                    f"Connected to eBay as {who}. The first sync (up to 2 years of history) is running in the background.")


@router.get("/ebay/callback")
def ebay_callback(request: Request, db: Session = Depends(get_db)):
    if request.query_params.get("error"):
        return redirect("/settings/ebay", request, f"eBay returned: {request.query_params.get('error_description') or request.query_params.get('error')}", "err")
    return _finish(request, db, request.query_params.get("code"), request.query_params.get("state"), True)


@router.post("/ebay/manual-code")
async def ebay_manual(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    code, state = ebay.extract_code(f.get("pasted") or "")
    return _finish(request, db, code, state, check_state=bool(state))


@router.get("/ebay/declined")
def ebay_declined(request: Request):
    return redirect("/settings/ebay", request, "Authorization was declined on eBay.", "err")


@router.post("/settings/ebay/disconnect")
async def ebay_disconnect(request: Request, db: Session = Depends(get_db)):
    await form(request)
    acct = db.query(EbayAccount).first()
    if acct:
        db.delete(acct)
        db.commit()
    return redirect("/settings/ebay", request, "eBay disconnected. Your synced data was kept.")


@router.post("/sync/{job}")
async def run_now(job: str, request: Request):
    await form(request)
    if job not in scheduler.JOBS:
        raise HTTPException(404)
    scheduler.run_async(job)
    back = request.headers.get("referer") or "/settings/ebay"
    return redirect(back, request, f"Started {job.replace('_', ' ')} — refresh in a minute to see results.")


# ------------------------------------------------------------------ public endpoints
@router.get("/ebay/account-deletion")
def deletion_challenge(request: Request):
    code = request.query_params.get("challenge_code")
    if not code:
        return JSONResponse({"status": "ready"})
    token = settings.get("EBAY_VERIFICATION_TOKEN")
    endpoint = endpoints(request)["deletion"]
    return JSONResponse({"challengeResponse": ebay.challenge_response(code, token, endpoint)})


@router.post("/ebay/account-deletion")
async def deletion_notice(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    verified = ebay.verify_notification_signature(raw, request.headers.get("x-ebay-signature"))
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return Response(status_code=400)
    data = (payload.get("notification") or {}).get("data") or {}
    username = data.get("username")
    n = 0
    if username and verified:
        for s in db.query(Sale).filter(Sale.buyer_username == username).all():
            s.buyer_username = None
            s.buyer_city = None
            s.buyer_postal = None
            n += 1
    db.add(DeletionNotice(notification_id=(payload.get("notification") or {}).get("notificationId"),
                          ebay_username=None, verified=verified, records_redacted=n))
    db.commit()
    # Always acknowledge; unverified notices are logged but not acted on.
    return Response(status_code=204)


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "privacy.html")
