from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..db import get_db
from ..models import BulkBuy, Item, ItemEvent, Listing, Sale
from ..services import automations
from ..services.sync import add_event, refresh_item_status
from ..util import parse_date, to_float, utcnow
from ..web import form, redirect, render

router = APIRouter()
UPLOADS = config.DATA_DIR / "uploads"
PAGE = 50


async def save_upload(upload, sub: str) -> str | None:
    if not upload or not getattr(upload, "filename", None):
        return None
    ext = Path(upload.filename).suffix.lower()[:8]
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".pdf"):
        raise HTTPException(400, "Unsupported file type")
    d = UPLOADS / sub
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    data = await upload.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "File too large (15 MB max)")
    (d / name).write_bytes(data)
    return f"{sub}/{name}"


@router.get("/uploads/{path:path}")
def uploads(path: str):
    p = (UPLOADS / path).resolve()
    if not str(p).startswith(str(UPLOADS.resolve())) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


def _sources(db: Session) -> list[str]:
    return [r[0] for r in db.query(Item.source).filter(Item.source.isnot(None)).distinct().order_by(Item.source).all()]


def _locations(db: Session) -> list[str]:
    return [r[0] for r in db.query(Item.storage_location).filter(Item.storage_location.isnot(None))
            .distinct().order_by(Item.storage_location).all()]


def _filtered(db: Session, p) -> tuple:
    q = db.query(Item)
    status = p.get("status") or "active"
    if status == "active":
        q = q.filter(Item.status.in_(("unlisted", "listed")))
    elif status != "all":
        q = q.filter(Item.status == status)
    if p.get("q"):
        like = f"%{p['q'].strip()}%"
        q = q.filter(or_(Item.title.ilike(like), Item.sku.ilike(like), Item.current_ebay_item_id.ilike(like),
                         Item.storage_location.ilike(like), Item.source.ilike(like)))
    if p.get("source"):
        q = q.filter(Item.source == p["source"])
    if p.get("location"):
        q = q.filter(Item.storage_location == p["location"])
    missing = p.get("missing")
    if missing == "cost":
        q = q.filter(Item.cost.is_(None))
    elif missing == "date":
        q = q.filter(Item.purchase_date.is_(None), Item.bulk_buy_id.is_(None))
    elif missing == "source":
        q = q.filter(Item.source.is_(None), Item.bulk_buy_id.is_(None))
    elif missing == "location":
        q = q.filter(Item.storage_location.is_(None))
    if p.get("year") and status == "sold":
        y = int(p["year"])
        q = q.filter(Item.sold_at >= datetime(y, 1, 1), Item.sold_at < datetime(y + 1, 1, 1))
    if p.get("bulk"):
        q = q.filter(Item.bulk_buy_id == int(p["bulk"]))
    sort = p.get("sort") or "newest"
    order = {
        "newest": Item.id.desc(), "oldest": Item.id.asc(), "title": Item.title.asc(),
        "cost": Item.cost.desc(), "listed": Item.listed_at.asc(), "sold": Item.sold_at.desc(),
    }.get(sort, Item.id.desc())
    return q.order_by(order), status


@router.get("/inventory")
def inventory(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    q, status = _filtered(db, p)
    page = max(int(p.get("page", 1) or 1), 1)
    total = q.count()
    items = q.options(selectinload(Item.listings)).offset((page - 1) * PAGE).limit(PAGE).all()
    counts = dict(db.query(Item.status, func.count(Item.id)).group_by(Item.status).all())
    return render(request, "inventory.html", {
        "items": items, "p": p, "status": status, "page": page, "pages": max((total - 1) // PAGE + 1, 1),
        "total": total, "counts": counts, "sources": _sources(db), "locations": _locations(db)})


@router.get("/inventory/quick-edit")
def quick_edit(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    p.setdefault("missing", "cost")
    q, status = _filtered(db, p)
    items = q.limit(100).all()
    return render(request, "quick_edit.html", {"items": items, "p": p, "status": status,
                                               "sources": _sources(db), "locations": _locations(db)})


@router.post("/inventory/quick-edit")
async def quick_edit_save(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    ids = [int(x) for x in f.getlist("id")]
    n = 0
    for iid in ids:
        it = db.get(Item, iid)
        if not it:
            continue
        changed = False
        for field, conv in (("cost", lambda v: to_float(v, None)), ("source", lambda v: v.strip() or None),
                            ("purchase_date", parse_date), ("storage_location", lambda v: v.strip() or None)):
            raw = f.get(f"{field}_{iid}")
            if raw is None or raw == "":
                continue
            val = conv(raw)
            if getattr(it, field) != val:
                setattr(it, field, val)
                changed = True
        n += changed
    db.commit()
    back = f.get("back") or "/inventory/quick-edit"
    return redirect(back, request, f"Updated {n} items.")


def _apply_item_form(it: Item, f) -> None:
    it.title = (f.get("title") or it.title or "Untitled").strip()
    it.sku = (f.get("sku") or "").strip() or None
    it.quantity = max(int(to_float(f.get("quantity"), 1) or 1), 1)
    it.cost = to_float(f.get("cost"), None)
    it.source = (f.get("source") or "").strip() or None
    it.purchase_date = parse_date(f.get("purchase_date"))
    it.storage_location = (f.get("storage_location") or "").strip() or None
    it.category = (f.get("category") or "").strip() or None
    it.brand = (f.get("brand") or "").strip() or None
    it.condition = (f.get("condition") or "").strip() or None
    it.notes = (f.get("notes") or "").strip() or None
    it.min_price = to_float(f.get("min_price"), None)
    it.relist_enabled = f.get("relist_enabled") == "on"
    it.reprice_enabled = f.get("reprice_enabled") == "on"
    it.promote_enabled = f.get("promote_enabled") == "on"
    bb = f.get("bulk_buy_id")
    it.bulk_buy_id = int(bb) if bb else None


@router.get("/inventory/new")
def item_new(request: Request, db: Session = Depends(get_db)):
    it = Item(title="", quantity=1, relist_enabled=True, reprice_enabled=True, promote_enabled=True,
              purchase_date=utcnow().date())
    bb = request.query_params.get("bulk")
    if bb:
        it.bulk_buy_id = int(bb)
    return render(request, "item_form.html", {"it": it, "new": True, "sources": _sources(db),
                                              "locations": _locations(db), "bulk_buys": db.query(BulkBuy).order_by(BulkBuy.id.desc()).all()})


@router.post("/inventory/new")
async def item_create(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    it = Item(status="unlisted")
    _apply_item_form(it, f)
    it.photo = await save_upload(f.get("photo"), "items")
    db.add(it)
    db.flush()
    add_event(db, it, "created", "Added to inventory")
    db.commit()
    if f.get("again"):
        return redirect(f"/inventory/new" + (f"?bulk={it.bulk_buy_id}" if it.bulk_buy_id else ""), request,
                        f"Added “{it.title}”. Add another.")
    return redirect(f"/inventory/{it.id}", request, "Item added.")


@router.get("/inventory/{iid}")
def item_detail(iid: int, request: Request, db: Session = Depends(get_db)):
    it = db.get(Item, iid)
    if not it:
        raise HTTPException(404)
    events = db.query(ItemEvent).filter_by(item_id=iid).order_by(ItemEvent.ts.desc()).all()
    unlinked = db.query(Listing).filter(Listing.item_id.is_(None), Listing.status == "active").order_by(Listing.title).all()
    active = [lst for lst in it.listings if lst.status == "active"]
    next_price = automations.reprice_target(it, active[0]) if active else None
    from ..models import Location
    return render(request, "item_detail.html", {"it": it, "events": events, "unlinked": unlinked,
                                                "next_price": next_price,
                                                "all_locations": db.query(Location).order_by(Location.name).all()})


@router.get("/inventory/{iid}/edit")
def item_edit(iid: int, request: Request, db: Session = Depends(get_db)):
    it = db.get(Item, iid)
    if not it:
        raise HTTPException(404)
    return render(request, "item_form.html", {"it": it, "new": False, "sources": _sources(db),
                                              "locations": _locations(db), "bulk_buys": db.query(BulkBuy).order_by(BulkBuy.id.desc()).all()})


@router.post("/inventory/{iid}/edit")
async def item_update(iid: int, request: Request, db: Session = Depends(get_db)):
    it = db.get(Item, iid)
    if not it:
        raise HTTPException(404)
    f = await form(request)
    old_cost = it.cost
    _apply_item_form(it, f)
    photo = await save_upload(f.get("photo"), "items")
    if photo:
        it.photo = photo
    if old_cost != it.cost:
        add_event(db, it, "cost", f"Cost set to {it.cost if it.cost is not None else '—'}")
    db.flush()
    refresh_item_status(db, it)
    db.commit()
    return redirect(f"/inventory/{iid}", request, "Saved.")


@router.post("/inventory/{iid}/delete")
async def item_delete(iid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    it = db.get(Item, iid)
    if it:
        for lst in it.listings:
            lst.item_id = None
        for s in it.sales:
            s.item_id = None
        db.delete(it)
        db.commit()
    return redirect("/inventory", request, "Item deleted.")


@router.post("/inventory/{iid}/writeoff")
async def item_writeoff(iid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    it = db.get(Item, iid)
    if not it:
        raise HTTPException(404)
    if it.status == "written_off":
        it.status = "unlisted"
        it.written_off_at = None
        add_event(db, it, "restore", "Restored to inventory")
        refresh_item_status(db, it)
        msg = "Item restored."
    else:
        it.status = "written_off"
        it.written_off_at = parse_date(f.get("date")) or utcnow().date()
        add_event(db, it, "written_off", f"Written off (lost/damaged/donated): {f.get('reason') or ''}")
        msg = "Item written off — its cost moves to cost of goods sold."
    db.commit()
    return redirect(f"/inventory/{iid}", request, msg)


@router.post("/inventory/{iid}/link")
async def item_link(iid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    it = db.get(Item, iid)
    lst = db.query(Listing).filter_by(ebay_item_id=(f.get("ebay_item_id") or "").strip()).first()
    if not it or not lst:
        return redirect(f"/inventory/{iid}", request, "Listing not found — sync listings first.", "err")
    old = lst.item
    lst.item_id = it.id
    it.current_ebay_item_id = lst.ebay_item_id
    it.original_price = it.original_price or lst.price
    it.image_url = it.image_url or lst.image_url
    for s in db.query(Sale).filter_by(ebay_item_id=lst.ebay_item_id).all():
        s.item_id = it.id
    db.flush()
    if old and old.id != it.id and not old.cost and not old.sales and len(old.listings) <= 1:
        # auto-created placeholder from eBay — merge its timeline into this item
        for ev in db.query(ItemEvent).filter_by(item_id=old.id).all():
            ev.item_id = it.id
        db.flush()
        db.delete(old)
    add_event(db, it, "linked", f"Linked to eBay listing {lst.ebay_item_id}", lst.ebay_item_id)
    db.flush()
    db.refresh(it)
    refresh_item_status(db, it)
    db.commit()
    return redirect(f"/inventory/{iid}", request, "Linked to listing.")


@router.post("/inventory/{iid}/note")
async def item_note(iid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    it = db.get(Item, iid)
    if it and (f.get("note") or "").strip():
        add_event(db, it, "note", f.get("note").strip())
        db.commit()
    return redirect(f"/inventory/{iid}")


@router.post("/inventory/{iid}/merge")
async def item_merge(iid: int, request: Request, db: Session = Depends(get_db)):
    """Merge another item (e.g. an eBay placeholder) into this one."""
    f = await form(request)
    it = db.get(Item, iid)
    other = db.get(Item, int(to_float(f.get("other_id"), 0) or 0))
    if not it or not other or other.id == it.id:
        return redirect(f"/inventory/{iid}", request, "Item to merge not found.", "err")
    for lst in list(other.listings):
        lst.item_id = it.id
    for s in list(other.sales):
        s.item_id = it.id
    for ev in db.query(ItemEvent).filter_by(item_id=other.id).all():
        ev.item_id = it.id
    for field in ("cost", "source", "purchase_date", "storage_location", "sku", "image_url", "original_price"):
        if getattr(it, field) is None and getattr(other, field) is not None:
            setattr(it, field, getattr(other, field))
    db.flush()
    db.delete(other)
    db.flush()
    db.refresh(it)
    add_event(db, it, "merged", f"Merged item #{other.id} into this item")
    refresh_item_status(db, it)
    db.commit()
    return redirect(f"/inventory/{iid}", request, "Items merged.")


# ------------------------------------------------------------------ bulk buys
@router.get("/bulk-buys")
def bulk_list(request: Request, db: Session = Depends(get_db)):
    rows = []
    for bb in db.query(BulkBuy).options(selectinload(BulkBuy.items).selectinload(Item.sales)).order_by(BulkBuy.id.desc()).all():
        sales = [s for it in bb.items for s in it.sales if s.status != "cancelled"]
        net_before = sum(s.net_before_cogs for s in sales)
        rows.append({"bb": bb, "items": len(bb.items), "sold": sum(1 for it in bb.items if it.status == "sold"),
                     "revenue": sum(s.gross for s in sales), "net_before": net_before,
                     "profit": net_before - (bb.total_cost or 0),
                     "roi": ((net_before - (bb.total_cost or 0)) / bb.total_cost * 100) if bb.total_cost else None})
    return render(request, "bulk_buys.html", {"rows": rows, "sources": _sources(db), "today": utcnow().date()})


@router.post("/bulk-buys/new")
async def bulk_create(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    bb = BulkBuy(name=(f.get("name") or "Bulk buy").strip(), purchase_date=parse_date(f.get("purchase_date")),
                 total_cost=to_float(f.get("total_cost")) or 0, source=(f.get("source") or "").strip() or None,
                 notes=(f.get("notes") or "").strip() or None)
    db.add(bb)
    db.flush()
    titles = [t.strip() for t in (f.get("titles") or "").splitlines() if t.strip()]
    for t in titles:
        it = Item(title=t, bulk_buy_id=bb.id, source=bb.source, purchase_date=bb.purchase_date, status="unlisted")
        db.add(it)
        db.flush()
        add_event(db, it, "created", f"Added from bulk buy “{bb.name}”")
    db.commit()
    if titles:
        _allocate(db, bb, "even")
    return redirect(f"/bulk-buys/{bb.id}", request, "Bulk buy created.")


def _allocate(db: Session, bb: BulkBuy, method: str) -> None:
    items = list(bb.items)
    if not items:
        return
    total = bb.total_cost or 0
    if method == "value":
        weights = [max((it.original_price or (it.listings[0].price if it.listings else 0) or 0), 0) for it in items]
        if not sum(weights):
            weights = [1] * len(items)
    else:
        weights = [it.quantity or 1 for it in items]
    s = sum(weights)
    running = 0.0
    for i, (it, w) in enumerate(zip(items, weights)):
        share = round(total * w / s, 2) if i < len(items) - 1 else round(total - running, 2)
        running += share
        it.cost = share
    db.commit()


@router.get("/bulk-buys/{bid}")
def bulk_detail(bid: int, request: Request, db: Session = Depends(get_db)):
    bb = db.get(BulkBuy, bid)
    if not bb:
        raise HTTPException(404)
    sales = [s for it in bb.items for s in it.sales if s.status != "cancelled"]
    net_before = sum(s.net_before_cogs for s in sales)
    stats = {"revenue": sum(s.gross for s in sales), "net_before": net_before,
             "profit": net_before - (bb.total_cost or 0),
             "allocated": sum(it.cost or 0 for it in bb.items),
             "roi": ((net_before - (bb.total_cost or 0)) / bb.total_cost * 100) if bb.total_cost else None,
             "sold": sum(1 for it in bb.items if it.status == "sold")}
    timeline = sorted(sales, key=lambda s: s.sale_date)
    cum, points = 0.0, []
    for s in timeline:
        cum += s.net_before_cogs
        points.append({"x": s.sale_date.strftime("%Y-%m-%d"), "y": round(cum - (bb.total_cost or 0), 2)})
    return render(request, "bulk_detail.html", {"bb": bb, "stats": stats, "points": points,
                                                "sources": _sources(db)})


@router.post("/bulk-buys/{bid}/edit")
async def bulk_update(bid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    bb = db.get(BulkBuy, bid)
    if not bb:
        raise HTTPException(404)
    bb.name = (f.get("name") or bb.name).strip()
    bb.purchase_date = parse_date(f.get("purchase_date"))
    bb.total_cost = to_float(f.get("total_cost")) or 0
    bb.source = (f.get("source") or "").strip() or None
    bb.notes = (f.get("notes") or "").strip() or None
    for it in bb.items:
        it.source = it.source or bb.source
        it.purchase_date = it.purchase_date or bb.purchase_date
    db.commit()
    return redirect(f"/bulk-buys/{bid}", request, "Saved.")


@router.post("/bulk-buys/{bid}/allocate")
async def bulk_allocate(bid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    bb = db.get(BulkBuy, bid)
    if bb:
        _allocate(db, bb, f.get("method") or "even")
    return redirect(f"/bulk-buys/{bid}", request, "Cost allocated across items.")


@router.post("/bulk-buys/{bid}/add-items")
async def bulk_add_items(bid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    bb = db.get(BulkBuy, bid)
    if not bb:
        raise HTTPException(404)
    n = 0
    for t in (f.get("titles") or "").splitlines():
        if t.strip():
            it = Item(title=t.strip(), bulk_buy_id=bb.id, source=bb.source, purchase_date=bb.purchase_date,
                      status="unlisted")
            db.add(it)
            db.flush()
            add_event(db, it, "created", f"Added from bulk buy “{bb.name}”")
            n += 1
    for raw in (f.get("existing") or "").replace(",", " ").split():
        it = db.get(Item, int(to_float(raw, 0) or 0)) if raw.strip().isdigit() else \
            db.query(Item).filter((Item.sku == raw.strip()) | (Item.current_ebay_item_id == raw.strip())).first()
        if it:
            it.bulk_buy_id = bb.id
            it.source = it.source or bb.source
            it.purchase_date = it.purchase_date or bb.purchase_date
            n += 1
    db.commit()
    return redirect(f"/bulk-buys/{bid}", request, f"Added {n} items. Re-run cost allocation if needed.")


@router.post("/bulk-buys/{bid}/delete")
async def bulk_delete(bid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    bb = db.get(BulkBuy, bid)
    if bb:
        for it in bb.items:
            it.bulk_buy_id = None
        db.delete(bb)
        db.commit()
    return redirect("/bulk-buys", request, "Bulk buy deleted (items kept).")


# ------------------------------------------------------------------ listings
@router.get("/listings")
def listings(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    q = db.query(Listing).options(selectinload(Listing.item))
    show = p.get("show") or "active"
    if show == "active":
        q = q.filter(or_(Listing.status == "active", Listing.pending_relist.is_(True)))
    elif show != "all":
        q = q.filter(Listing.status == show)
    if p.get("q"):
        like = f"%{p['q'].strip()}%"
        q = q.filter(or_(Listing.title.ilike(like), Listing.ebay_item_id.ilike(like), Listing.sku.ilike(like)))
    if p.get("unlinked"):
        q = q.join(Item, isouter=True).filter(or_(Listing.item_id.is_(None), Item.cost.is_(None)))
    sort = p.get("sort") or "renew"
    q = q.order_by({"renew": Listing.end_time.asc(), "oldest": Listing.start_time.asc(),
                    "newest": Listing.start_time.desc(), "price": Listing.price.desc(),
                    "watchers": Listing.watch_count.desc()}.get(sort, Listing.end_time.asc()))
    page = max(int(p.get("page", 1) or 1), 1)
    total = q.count()
    rows = q.offset((page - 1) * PAGE).limit(PAGE).all()
    now = utcnow()
    return render(request, "listings.html", {"rows": rows, "p": p, "show": show, "now": now, "page": page,
                                             "pages": max((total - 1) // PAGE + 1, 1), "total": total,
                                             "soon": now + timedelta(hours=24)})
