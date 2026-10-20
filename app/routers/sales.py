from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import FinanceTransaction, Item, Sale
from ..services.reports import MARKETPLACES, period_range, summarize
from ..services.sync import add_event, refresh_item_status
from ..util import parse_date, parse_iso, to_float, utcnow
from ..web import form, redirect, render

router = APIRouter()
PAGE = 50


@router.get("/sales")
def sales_list(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    period = p.get("period") or "90d"
    start, end, label = period_range(period, p.get("start"), p.get("end"), db)
    q = db.query(Sale).options(selectinload(Sale.item))
    if not p.get("q"):
        q = q.filter(Sale.sale_date >= datetime.combine(start, datetime.min.time()),
                     Sale.sale_date <= datetime.combine(end, datetime.max.time()))
    else:
        like = f"%{p['q'].strip()}%"
        q = q.filter(or_(Sale.title.ilike(like), Sale.order_id.ilike(like), Sale.sku.ilike(like),
                         Sale.ebay_item_id.ilike(like)))
    if p.get("marketplace"):
        q = q.filter(Sale.marketplace == p["marketplace"])
    if p.get("status"):
        q = q.filter(Sale.status == p["status"])
    if p.get("unlinked"):
        q = q.filter(Sale.item_id.is_(None))
    all_rows = q.order_by(Sale.sale_date.desc()).all()
    if p.get("missing") == "cost":
        all_rows = [s for s in all_rows if s.cogs is None and s.status != "cancelled"]
    page = max(int(p.get("page", 1) or 1), 1)
    rows = all_rows[(page - 1) * PAGE: page * PAGE]
    mkts = sorted({m for (m,) in db.query(Sale.marketplace).distinct().all()})
    return render(request, "sales.html", {"rows": rows, "p": p, "period": period, "label": label,
                                          "summary": summarize(all_rows), "marketplaces": mkts, "page": page,
                                          "pages": max((len(all_rows) - 1) // PAGE + 1, 1), "start": start, "end": end})


def _available_items(db: Session):
    return db.query(Item).filter(Item.status.in_(("unlisted", "listed"))).order_by(Item.title).limit(2000).all()


@router.get("/sales/new")
def sale_new(request: Request, db: Session = Depends(get_db)):
    iid = request.query_params.get("item")
    item = db.get(Item, int(iid)) if iid else None
    s = Sale(marketplace="Poshmark", quantity=1, sale_date=utcnow(), item_id=item.id if item else None,
             title=item.title if item else "", sale_price=0, shipping_charged=0, marketplace_fees=0, ad_fees=0,
             shipping_cost=0, other_fees=0, tax_collected=0, refund_amount=0)
    return render(request, "sale_form.html", {"s": s, "new": True, "items": _available_items(db),
                                              "marketplaces": MARKETPLACES, "item": item})


def _apply_sale_form(db: Session, s: Sale, f, manual: bool) -> None:
    if manual:
        s.marketplace = (f.get("marketplace") or "Other").strip()
        s.order_id = (f.get("order_id") or "").strip() or s.order_id or f"manual-{uuid.uuid4().hex[:10]}"
        d = parse_date(f.get("sale_date"))
        s.sale_date = datetime.combine(d, datetime.min.time().replace(hour=12)) if d else (s.sale_date or utcnow())
        s.quantity = max(int(to_float(f.get("quantity"), 1) or 1), 1)
        s.sale_price = to_float(f.get("sale_price")) or 0
        s.shipping_charged = to_float(f.get("shipping_charged")) or 0
        s.tax_collected = to_float(f.get("tax_collected")) or 0
        s.marketplace_fees = to_float(f.get("marketplace_fees")) or 0
        s.ad_fees = to_float(f.get("ad_fees")) or 0
        s.shipping_cost = to_float(f.get("shipping_cost")) or 0
        s.refund_amount = to_float(f.get("refund_amount")) or 0
        s.status = f.get("status") or "completed"
        s.buyer_city = (f.get("buyer_city") or "").strip() or None
        s.buyer_state = (f.get("buyer_state") or "").strip() or None
        s.buyer_country = (f.get("buyer_country") or "").strip().upper()[:2] or None
    s.other_fees = to_float(f.get("other_fees")) or 0
    s.cogs_override = to_float(f.get("cogs_override"), None)
    s.restocked = f.get("restocked") == "on"
    s.notes = (f.get("notes") or "").strip() or None
    iid = (f.get("item_id") or "").split(" ")[0].lstrip("#")
    new_item = db.get(Item, int(iid)) if iid.isdigit() else None
    s.item_id = new_item.id if new_item else None
    s.item = new_item
    if manual:
        s.title = (f.get("title") or "").strip() or (new_item.title if new_item else "Sale")


@router.post("/sales/new")
async def sale_create(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    s = Sale(source="manual", line_item_id="1")
    _apply_sale_form(db, s, f, manual=True)
    if s.item_id is None and f.get("create_item") == "on":
        it = Item(title=s.title, cost=to_float(f.get("item_cost"), None), quantity=s.quantity,
                  source=(f.get("item_source") or "").strip() or None, status="sold",
                  purchase_date=parse_date(f.get("item_purchase_date")))
        db.add(it)
        db.flush()
        add_event(db, it, "created", "Created with a manual sale")
        s.item_id = it.id
        s.item = it
    db.add(s)
    db.flush()
    if s.item:
        add_event(db, s.item, "sold", f"Sold on {s.marketplace} for {s.sale_price:.2f}")
        db.flush()
        db.refresh(s.item)
        refresh_item_status(db, s.item)
    db.commit()
    return redirect(f"/sales/{s.id}", request, "Sale recorded.")


@router.get("/sales/{sid}")
def sale_detail(sid: int, request: Request, db: Session = Depends(get_db)):
    s = db.get(Sale, sid)
    if not s:
        raise HTTPException(404)
    txns = db.query(FinanceTransaction).filter_by(order_id=s.order_id).order_by(
        FinanceTransaction.transaction_date).all() if s.marketplace == "eBay" else []
    siblings = db.query(Sale).filter(Sale.order_id == s.order_id, Sale.id != s.id,
                                     Sale.marketplace == s.marketplace).all()
    return render(request, "sale_detail.html", {"s": s, "txns": txns, "siblings": siblings,
                                                "items": _available_items(db), "marketplaces": MARKETPLACES})


@router.post("/sales/{sid}/edit")
async def sale_update(sid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    s = db.get(Sale, sid)
    if not s:
        raise HTTPException(404)
    old_item = s.item
    _apply_sale_form(db, s, f, manual=(s.source != "ebay_sync"))
    db.flush()
    for it in {old_item, s.item} - {None}:
        db.refresh(it)
        refresh_item_status(db, it)
    db.commit()
    return redirect(f"/sales/{sid}", request, "Sale updated.")


@router.post("/sales/{sid}/delete")
async def sale_delete(sid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    s = db.get(Sale, sid)
    if not s:
        raise HTTPException(404)
    if s.source == "ebay_sync":
        return redirect(f"/sales/{sid}", request, "eBay sales are managed by sync and can't be deleted.", "err")
    it = s.item
    db.delete(s)
    db.flush()
    if it:
        db.refresh(it)
        refresh_item_status(db, it)
    db.commit()
    return redirect("/sales", request, "Sale deleted.")
