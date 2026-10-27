from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Item, Location
from ..services import labels as L
from ..services import locations as locsvc
from ..util import to_float, utcnow
from ..web import flash, form, redirect, render

router = APIRouter()


def _get(db: Session, code: str) -> Location:
    loc = locsvc.by_code(db, code)
    if not loc:
        raise HTTPException(404, "No location with that code.")
    return loc


def _in_stock(q):
    return q.filter(Item.status.in_(("unlisted", "listed")))


# ------------------------------------------------------------------ list / create
@router.get("/locations")
def locations(request: Request, db: Session = Depends(get_db)):
    q = (request.query_params.get("q") or "").strip()
    query = db.query(Location)
    if q:
        query = query.filter(or_(Location.name.ilike(f"%{q}%"), Location.code.ilike(f"%{q}%")))
    locs = query.order_by(Location.name).all()
    counts = dict(_in_stock(db.query(Item.location_id, func.count(Item.id))).group_by(Item.location_id).all())
    unplaced = _in_stock(db.query(Item)).filter(Item.location_id.is_(None)).count()
    return render(request, "locations.html", {"locs": locs, "counts": counts, "q": q, "unplaced": unplaced,
                                              "layouts": L.LAYOUTS, "default_layout": L.DEFAULT_LAYOUT})


@router.post("/locations/new")
async def locations_new(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    names = [n.strip() for n in (f.get("names") or "").splitlines() if n.strip()]
    # Range helper: "Shelf A1-A12" or "Bin 1-20"
    expanded = []
    for n in names:
        m = re.match(r"^(.*?)(\d+)\s*-\s*([A-Za-z]*)(\d+)$", n)
        if m and int(m.group(4)) - int(m.group(2)) <= 200 and int(m.group(4)) >= int(m.group(2)):
            prefix, a, b = m.group(1), int(m.group(2)), int(m.group(4))
            width = len(m.group(2)) if m.group(2).startswith("0") else 0
            expanded += [f"{prefix}{str(i).zfill(width)}" for i in range(a, b + 1)]
        else:
            expanded.append(n)
    created = []
    for n in expanded:
        if not locsvc.find_by_name(db, n):
            loc = locsvc.ensure(db, n)
            loc.description = (f.get("description") or "").strip() or None
            db.flush()
            created.append(loc)
    db.commit()
    if f.get("print") and created:
        ids = ",".join(str(x.id) for x in created)
        return redirect(f"/locations/labels?ids={ids}")
    return redirect("/locations", request, f"Created {len(created)} location(s)." if created else "Those locations already exist.")


# ------------------------------------------------------------------ labels
def _label_data(request: Request, db: Session) -> tuple[list[dict], dict, dict]:
    p = request.query_params
    ids = [int(x) for x in (p.get("ids") or "").replace(" ", "").split(",") if x.isdigit()]
    if p.get("all"):
        locs = db.query(Location).order_by(Location.name).all()
    else:
        locs = db.query(Location).filter(Location.id.in_(ids)).order_by(Location.name).all() if ids else []
    base = locsvc.public_base(request)
    items = [{"name": l.name, "code": l.code, "sub": l.description or "", "url": locsvc.qr_url(base, l.code)} for l in locs]
    opts = {"layout": p.get("layout") or L.DEFAULT_LAYOUT, "copies": int(to_float(p.get("copies"), 1) or 1),
            "skip": int(to_float(p.get("skip"), 0) or 0), "cut": p.get("cut", "1") != "0",
            "ids": ",".join(str(l.id) for l in locs), "all": p.get("all")}
    return L.expand(items, opts["copies"]), L.layout(opts["layout"]), opts


@router.get("/locations/labels")
def labels_page(request: Request, db: Session = Depends(get_db)):
    labs, lay, opts = _label_data(request, db)
    if not labs:
        return redirect("/locations", request, "Pick at least one location to print.", "err")
    cells = []
    for page in L.pages(labs, lay["per_page"], opts["skip"]):
        cells.append([None if lab is None else {**lab, "svg": L.qr_svg(lab["url"]), **L.sizes_for(lay, lab["name"], lab["sub"])}
                      for lab in page])
    return render(request, "labels_print.html", {"pages": cells, "lay": lay, "opts": opts, "layouts": L.LAYOUTS,
                                                 "count": len(labs)})


@router.get("/locations/labels.pdf")
def labels_pdf(request: Request, db: Session = Depends(get_db)):
    labs, lay, opts = _label_data(request, db)
    if not labs:
        raise HTTPException(400, "No locations selected.")
    data = L.pdf(labs, lay["key"], opts["skip"], opts["cut"])
    name = f"flipdesk-labels-{lay['key']}-{utcnow():%Y%m%d}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


# ------------------------------------------------------------------ scanning
@router.get("/l/{code}")
@router.get("/L/{code}")  # labels encode the URL in upper case (compact QR alphanumeric mode)
def short_link(code: str, request: Request, db: Session = Depends(get_db)):
    """What a phone camera opens when it scans a shelf label."""
    loc = _get(db, code)
    pending = request.session.pop("scan_item", None)
    # a "scan shelf for this item" was started in the app within 5 minutes, then the phone camera was used
    if pending and time.time() - pending.get("t", 0) < 300:
        it = db.get(Item, pending.get("id"))
        if it:
            locsvc.set_item_location(db, it, loc, "scanned")
            db.commit()
            return redirect(f"/inventory/{it.id}", request, f"📍 Moved to {loc.name}.")
    return redirect(f"/locations/{loc.code}")


@router.get("/scan")
def scan_page(request: Request, db: Session = Depends(get_db)):
    mode = request.query_params.get("mode") or "find"
    item = None
    if mode == "item":
        iid = request.query_params.get("item")
        item = db.get(Item, int(iid)) if iid and iid.isdigit() else None
        if not item:
            return redirect("/inventory", request, "Item not found.", "err")
        request.session["scan_item"] = {"id": item.id, "t": int(time.time())}
    else:
        request.session.pop("scan_item", None)
    return render(request, "scan.html", {"mode": mode, "item": item})


@router.post("/scan/resolve")
async def scan_resolve(request: Request, db: Session = Depends(get_db)):
    tok = request.headers.get("x-csrf-token")
    if not tok or tok != request.session.get("csrf"):
        return JSONResponse({"error": "Page expired — refresh and try again."}, 400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, 400)
    text = str(body.get("text") or "")
    code = locsvc.parse_scan(text)
    loc = locsvc.by_code(db, code) if code else None
    if loc is None and text.strip() and not code:
        loc = locsvc.find_by_name(db, text)  # typed a location name
    if loc is None:
        return JSONResponse({"error": f"That QR code isn't a {request.app.title} location label." if not code
                             else f"No location with code {code}."}, 404)
    mode = body.get("mode") or "find"
    if mode == "item":
        it = db.get(Item, int(body.get("item") or 0))
        if not it:
            return JSONResponse({"error": "Item not found."}, 404)
        changed = locsvc.set_item_location(db, it, loc, "scanned")
        db.commit()
        request.session.pop("scan_item", None)
        flash(request, f"📍 {it.title[:60]} → {loc.name}" if changed else f"Already in {loc.name}.")
        return JSONResponse({"redirect": f"/inventory/{it.id}", "location": loc.name})
    if mode == "put":
        return JSONResponse({"redirect": f"/locations/{loc.code}?put=1", "location": loc.name})
    return JSONResponse({"redirect": f"/locations/{loc.code}", "location": loc.name})


# ------------------------------------------------------------------ one location
@router.get("/locations/{code}")
def location_detail(code: str, request: Request, db: Session = Depends(get_db)):
    loc = _get(db, code)
    items = (db.query(Item).filter(Item.location_id == loc.id).options(selectinload(Item.listings))
             .order_by(Item.status, Item.title).all())
    here = [i for i in items if i.status in ("unlisted", "listed")]
    gone = [i for i in items if i.status not in ("unlisted", "listed")]
    q = (request.query_params.get("q") or "").strip()
    found = []
    if q or request.query_params.get("put"):
        sq = _in_stock(db.query(Item)).filter(or_(Item.location_id.is_(None), Item.location_id != loc.id))
        if q:
            like = f"%{q}%"
            sq = sq.filter(or_(Item.title.ilike(like), Item.sku.ilike(like), Item.current_ebay_item_id.ilike(like)))
        else:
            sq = sq.filter(Item.location_id.is_(None))  # default: things not on a shelf yet
        found = sq.order_by(Item.id.desc()).limit(60).all()
    base = locsvc.public_base(request)
    return render(request, "location_detail.html", {
        "loc": loc, "here": here, "gone": gone, "found": found, "q": q, "put": bool(request.query_params.get("put")) or bool(q),
        "qr": L.qr_svg(locsvc.qr_url(base, loc.code)), "url": locsvc.qr_url(base, loc.code),
        "layouts": L.LAYOUTS, "default_layout": L.DEFAULT_LAYOUT,
        "value": sum((i.unit_cost or 0) * max((i.quantity or 1) - (i.quantity_sold or 0), 0) for i in here)})


@router.post("/locations/{code}/assign")
async def location_assign(code: str, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    loc = _get(db, code)
    n = 0
    for iid in f.getlist("item"):
        it = db.get(Item, int(iid)) if str(iid).isdigit() else None
        if it and locsvc.set_item_location(db, it, loc):
            n += 1
    db.commit()
    return redirect(f"/locations/{loc.code}", request, f"Moved {n} item(s) to {loc.name}.")


@router.post("/locations/{code}/remove/{iid}")
async def location_remove(code: str, iid: int, request: Request, db: Session = Depends(get_db)):
    await form(request)
    loc = _get(db, code)
    it = db.get(Item, iid)
    if it and it.location_id == loc.id:
        locsvc.set_item_location(db, it, None)
        db.commit()
    return redirect(f"/locations/{loc.code}", request, "Removed from this location.")


@router.post("/locations/{code}/edit")
async def location_edit(code: str, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    loc = _get(db, code)
    name = (f.get("name") or "").strip()
    if not name:
        return redirect(f"/locations/{loc.code}", request, "Name can't be blank.", "err")
    other = locsvc.find_by_name(db, name)
    if other and other.id != loc.id:
        return redirect(f"/locations/{loc.code}", request, "Another location already has that name.", "err")
    locsvc.rename(db, loc, name)
    loc.description = (f.get("description") or "").strip() or None
    db.commit()
    return redirect(f"/locations/{loc.code}", request, "Saved. Labels keep working — the QR holds the code, not the name.")


@router.post("/locations/{code}/delete")
async def location_delete(code: str, request: Request, db: Session = Depends(get_db)):
    await form(request)
    loc = _get(db, code)
    for it in list(loc.items):
        it.location_id = None
        it.location = None
        it.storage_location = None
    db.delete(loc)
    db.commit()
    return redirect("/locations", request, f"Deleted {loc.name}. Its items now have no location.")


# ------------------------------------------------------------------ item → location (manual)
@router.post("/inventory/{iid}/location")
async def item_set_location(iid: int, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    it = db.get(Item, iid)
    if not it:
        raise HTTPException(404)
    text = (f.get("location") or "").strip()
    if not text:
        locsvc.set_item_location(db, it, None)
        msg = "Location cleared."
    else:
        code = locsvc.parse_scan(text)
        loc = (locsvc.by_code(db, code) if code else None) or locsvc.ensure(db, text)
        db.flush()
        locsvc.set_item_location(db, it, loc)
        msg = f"📍 Moved to {loc.name}."
    db.commit()
    return redirect(f"/inventory/{iid}", request, msg)
