"""Type-to-search linking between eBay listings and inventory items."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, Listing
from ..services import linking
from ..web import form, redirect

router = APIRouter()


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@router.get("/api/search/listings")
def api_search_listings(request: Request, db: Session = Depends(get_db)):
    p = request.query_params
    item_id = _int(p.get("item"))
    q = (p.get("q") or "").strip()
    if not q and item_id:
        it = db.get(Item, item_id)
        res = linking.suggest_listings(db, it) if it else []
        return JSONResponse({"results": res, "suggested": True})
    return JSONResponse({"results": linking.search_listings(db, q, exclude_item=item_id,
                                                            include_ended=p.get("ended") == "1"), "suggested": False})


@router.get("/api/search/items")
def api_search_items(request: Request, db: Session = Depends(get_db)):
    p = request.query_params
    q = (p.get("q") or "").strip()
    lst = db.query(Listing).filter_by(ebay_item_id=p.get("listing")).first() if p.get("listing") else None
    if not q and lst is not None:
        return JSONResponse({"results": linking.suggest_items(db, lst), "suggested": True})
    exclude = lst.item_id if (lst is not None and lst.item is not None and linking.is_placeholder(lst.item)) else None
    return JSONResponse({"results": linking.search_items(db, q, exclude=exclude, include_sold=p.get("sold") == "1"),
                         "suggested": False})


def _back(request: Request, default: str) -> str:
    nxt = request.query_params.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


@router.post("/inventory/{iid}/link")
async def item_link(iid: int, request: Request, db: Session = Depends(get_db)):
    """Inventory → eBay: link this item to a listing chosen by search (or typed item number)."""
    f = await form(request)
    it = db.get(Item, iid)
    if not it:
        raise HTTPException(404)
    ref = (f.get("ebay_item_id") or "").strip().lstrip("#")
    lst = db.query(Listing).filter_by(ebay_item_id=ref).first() if ref else None
    back = _back(request, f"/inventory/{iid}")
    if not lst:
        return redirect(back, request, "Pick a listing from the search results (sync listings first if it's new).", "err")
    msg = linking.link_listing(db, it, lst)
    db.commit()
    return redirect(back, request, f"🔗 “{it.title[:60]}” ⇄ eBay #{lst.ebay_item_id}. {msg}")


@router.post("/listings/{ebay_item_id}/link")
async def listing_link(ebay_item_id: str, request: Request, db: Session = Depends(get_db)):
    """eBay → inventory: link this listing to an inventory item chosen by search."""
    f = await form(request)
    lst = db.query(Listing).filter_by(ebay_item_id=ebay_item_id).first()
    if not lst:
        raise HTTPException(404)
    it = db.get(Item, _int((f.get("item_id") or "").lstrip("#")) or 0)
    back = _back(request, "/listings")
    if not it:
        return redirect(back, request, "Pick an inventory item from the search results.", "err")
    msg = linking.link_listing(db, it, lst)
    db.commit()
    return redirect(back, request, f"🔗 eBay #{lst.ebay_item_id} ⇄ “{it.title[:60]}”. {msg}")


# templates use this to decide between "Link to inventory" and "change"
from ..web import templates as _templates  # noqa: E402
_templates.env.globals["is_placeholder"] = linking.is_placeholder
