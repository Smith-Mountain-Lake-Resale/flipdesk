from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import settings
from ..db import get_db
from ..models import Expense, MileageEntry
from ..services.reports import EXPENSE_CATEGORIES, period_range
from ..util import parse_date, to_float, utcnow
from ..web import form, redirect, render
from .inventory import save_upload

router = APIRouter()


@router.get("/ledger")
def ledger(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    period = p.get("period") or "ytd"
    start, end, label = period_range(period, p.get("start"), p.get("end"), db)
    q = db.query(Expense).filter(Expense.expense_date >= start, Expense.expense_date <= end)
    if p.get("category"):
        q = q.filter(Expense.category == p["category"])
    rows = q.order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
    by_cat = defaultdict(float)
    for r in rows:
        by_cat[r.category] += r.amount
    edit = db.get(Expense, int(p["edit"])) if p.get("edit", "").isdigit() else None
    return render(request, "ledger.html", {"rows": rows, "p": p, "period": period, "label": label,
                                           "categories": EXPENSE_CATEGORIES, "by_cat": sorted(by_cat.items(), key=lambda kv: -kv[1]),
                                           "total": sum(r.amount for r in rows), "today": utcnow().date(), "edit": edit})


@router.post("/ledger/save")
async def ledger_save(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    eid = f.get("id")
    e = db.get(Expense, int(eid)) if eid else Expense(source="manual")
    if e is None:
        raise HTTPException(404)
    e.expense_date = parse_date(f.get("expense_date")) or utcnow().date()
    e.category = f.get("category") or "Other expenses"
    e.description = (f.get("description") or "").strip() or None
    e.vendor = (f.get("vendor") or "").strip() or None
    e.amount = to_float(f.get("amount")) or 0
    rec = await save_upload(f.get("receipt"), "receipts")
    if rec:
        e.receipt = rec
    if not eid:
        db.add(e)
    db.commit()
    return redirect(f"/ledger?period={f.get('period') or 'ytd'}", request, "Expense saved.")


@router.post("/ledger/{eid}/delete")
async def ledger_delete(eid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    e = db.get(Expense, eid)
    if e:
        db.delete(e)
        db.commit()
    return redirect("/ledger", request, "Expense deleted.")


@router.get("/mileage")
def mileage(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    period = p.get("period") or "ytd"
    start, end, label = period_range(period, p.get("start"), p.get("end"), db)
    rows = db.query(MileageEntry).filter(MileageEntry.trip_date >= start, MileageEntry.trip_date <= end) \
        .order_by(MileageEntry.trip_date.desc(), MileageEntry.id.desc()).all()
    total_miles = sum(r.total_miles for r in rows)
    deduction = sum(r.total_miles * settings.mileage_rate(r.trip_date.year) for r in rows)
    edit = db.get(MileageEntry, int(p["edit"])) if p.get("edit", "").isdigit() else None
    return render(request, "mileage.html", {"rows": rows, "p": p, "period": period, "label": label,
                                            "total_miles": total_miles, "deduction": deduction,
                                            "rate": settings.mileage_rate(utcnow().year), "today": utcnow().date(),
                                            "edit": edit, "rates": settings.get("MILEAGE_RATES")})


@router.post("/mileage/save")
async def mileage_save(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    mid = f.get("id")
    m = db.get(MileageEntry, int(mid)) if mid else MileageEntry()
    if m is None:
        raise HTTPException(404)
    m.trip_date = parse_date(f.get("trip_date")) or utcnow().date()
    m.miles = to_float(f.get("miles")) or 0
    m.round_trip = f.get("round_trip") == "on"
    m.purpose = (f.get("purpose") or "").strip() or None
    m.start_location = (f.get("start_location") or "").strip() or None
    m.end_location = (f.get("end_location") or "").strip() or None
    if not mid:
        db.add(m)
    db.commit()
    return redirect("/mileage", request, "Trip saved.")


@router.post("/mileage/{mid}/delete")
async def mileage_delete(mid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    m = db.get(MileageEntry, mid)
    if m:
        db.delete(m)
        db.commit()
    return redirect("/mileage", request, "Trip deleted.")
