"""Link eBay listings ⇄ inventory items, with type-to-search and suggested matches."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from ..models import Item, ItemEvent, Listing, Sale
from .sync import add_event, refresh_item_status

IN_STOCK = ("unlisted", "listed")


# ------------------------------------------------------------------ placeholders & merging
def is_placeholder(it: Item | None) -> bool:
    """An item FlipDesk created automatically from an eBay listing/order, with nothing the user added."""
    if it is None:
        return False
    return (it.cost is None and not it.source and it.purchase_date is None and it.bulk_buy_id is None
            and not it.notes and not it.photo and not it.storage_location and not it.category and not it.brand)


def merge_items(db: Session, keep: Item, drop: Item) -> None:
    """Move everything from `drop` into `keep` and delete `drop`."""
    if keep.id == drop.id:
        return
    # Re-point children through the relationships (not just the FK), otherwise deleting `drop`
    # would null out the listings/sales still held in its in-memory collections.
    for lst in list(drop.listings):
        lst.item = keep
    for sale in list(drop.sales):
        sale.item = keep
    for ev in db.query(ItemEvent).filter_by(item_id=drop.id).all():
        ev.item_id = keep.id
    for field in ("cost", "source", "purchase_date", "sku", "image_url", "original_price", "listed_at",
                  "category", "brand", "condition", "photo"):
        if getattr(keep, field) is None and getattr(drop, field) is not None:
            setattr(keep, field, getattr(drop, field))
    if not keep.storage_location and drop.storage_location:
        keep.storage_location = drop.storage_location
        keep.location_id = drop.location_id
    if (drop.quantity or 1) > (keep.quantity or 1) and keep.status != "sold":
        keep.quantity = drop.quantity
    db.flush()
    db.expire(drop, ["listings", "sales"])
    db.delete(drop)
    db.flush()


def link_listing(db: Session, item: Item, lst: Listing) -> str:
    """Attach an eBay listing (and its sales) to an inventory item. Returns a message."""
    old = lst.item
    if old is not None and old.id == item.id:
        return "Already linked."
    merged = False
    if old is not None and is_placeholder(old):
        merge_items(db, item, old)          # brings the whole relist chain, sales and timeline
        merged = True
    else:
        # move just this listing (and earlier IDs in its relist chain) + its sales
        chain, cur = [lst], lst
        while cur.previous_ebay_item_id:
            prev = db.query(Listing).filter_by(ebay_item_id=cur.previous_ebay_item_id).first()
            if not prev or prev in chain or (old is not None and prev.item_id != old.id):
                break
            chain.append(prev)
            cur = prev
        ids = [c.ebay_item_id for c in chain]
        for c in chain:
            c.item_id = item.id
        for s in db.query(Sale).filter(Sale.ebay_item_id.in_(ids)).all():
            s.item_id = item.id
    lst.item_id = item.id
    if lst.status == "active" or lst.pending_relist:
        item.current_ebay_item_id = lst.ebay_item_id
    item.original_price = item.original_price or lst.price
    item.image_url = item.image_url or lst.image_url
    if not item.sku and lst.sku:
        item.sku = lst.sku
    db.flush()
    db.refresh(item)
    add_event(db, item, "linked", f"Linked to eBay listing {lst.ebay_item_id}" +
              (f" (merged auto-created item #{old.id})" if merged else ""), lst.ebay_item_id)
    refresh_item_status(db, item)
    if old is not None and not merged:
        db.refresh(old)
        refresh_item_status(db, old)
    db.flush()
    return "Linked." + (" The item FlipDesk had auto-created for this listing was merged in." if merged else "")


# ------------------------------------------------------------------ search
def _words(q: str) -> list[str]:
    return [w for w in re.split(r"\s+", (q or "").strip()) if w][:8]


def _sim(a: str, b: str) -> float:
    a, b = (a or "").lower(), (b or "").lower()
    if not a or not b:
        return 0.0
    wa, wb = set(re.findall(r"[a-z0-9]+", a)), set(re.findall(r"[a-z0-9]+", b))
    overlap = len(wa & wb) / max(len(wa | wb), 1)
    return 0.6 * overlap + 0.4 * SequenceMatcher(None, a, b).ratio()


def _listing_json(l: Listing) -> dict:
    it = l.item
    linked = it is not None and not is_placeholder(it)
    sub = [f"eBay #{l.ebay_item_id}"]
    if l.price is not None:
        sub.append(f"${l.price:,.2f}")
    if l.sku:
        sub.append(l.sku)
    if l.status != "active":
        sub.append(l.status)
    return {"value": l.ebay_item_id, "label": l.title or l.ebay_item_id, "sub": " · ".join(sub), "img": l.image_url,
            "badge": f"linked to #{it.id}" if linked else None}


def search_listings(db: Session, q: str = "", exclude_item: int | None = None, include_ended: bool = False,
                    limit: int = 12) -> list[dict]:
    query = db.query(Listing).options(selectinload(Listing.item))
    if not include_ended:
        query = query.filter(or_(Listing.status == "active", Listing.pending_relist.is_(True)))
    if exclude_item:
        query = query.filter(or_(Listing.item_id.is_(None), Listing.item_id != exclude_item))
    for w in _words(q):
        like = f"%{w}%"
        query = query.filter(or_(Listing.title.ilike(like), Listing.ebay_item_id.ilike(like), Listing.sku.ilike(like)))
    rows = query.order_by(Listing.start_time.desc()).limit(200).all()
    # unlinked / placeholder-linked first, then closest title match
    ql = (q or "").lower()
    rows.sort(key=lambda l: (not (l.item is None or is_placeholder(l.item)),
                             0 if ql and (l.title or "").lower().startswith(ql) else 1, -_sim(q, l.title or "")))
    return [_listing_json(l) for l in rows[:limit]]


def suggest_listings(db: Session, item: Item, limit: int = 6) -> list[dict]:
    cands = (db.query(Listing).options(selectinload(Listing.item))
             .filter(or_(Listing.status == "active", Listing.pending_relist.is_(True)),
                     or_(Listing.item_id.is_(None), Listing.item_id != item.id)).limit(2000).all())
    cands = [l for l in cands if l.item is None or is_placeholder(l.item)]
    scored = sorted(((_sim(item.title, l.title), l) for l in cands), key=lambda x: -x[0])
    return [_listing_json(l) for s, l in scored[:limit] if s >= 0.2]


def _item_json(it: Item) -> dict:
    sub = []
    if it.sku:
        sub.append(it.sku)
    if it.storage_location:
        sub.append(f"📍 {it.storage_location}")
    if it.cost is not None:
        sub.append(f"cost ${it.cost:,.2f}")
    sub.append(it.status.replace("_", " "))
    sub.append(f"#{it.id}")
    active = [l for l in it.listings if l.status == "active"]
    return {"value": str(it.id), "label": it.title, "sub": " · ".join(sub),
            "img": f"/uploads/{it.photo}" if it.photo else it.image_url,
            "badge": f"on eBay #{active[0].ebay_item_id}" if active else ("auto-created" if is_placeholder(it) else None)}


def search_items(db: Session, q: str = "", exclude: int | None = None, include_sold: bool = False,
                 limit: int = 12) -> list[dict]:
    query = db.query(Item).options(selectinload(Item.listings))
    if not include_sold:
        query = query.filter(Item.status.in_(IN_STOCK))
    if exclude:
        query = query.filter(Item.id != exclude)
    for w in _words(q):
        like = f"%{w}%"
        conds = [Item.title.ilike(like), Item.sku.ilike(like), Item.storage_location.ilike(like)]
        if w.lstrip("#").isdigit():
            conds.append(Item.id == int(w.lstrip("#")))
        query = query.filter(or_(*conds))
    rows = query.order_by(Item.id.desc()).limit(300).all()
    ql = (q or "").lower()
    # real items (with cost/location) not already on eBay first, then best title match
    rows.sort(key=lambda it: (is_placeholder(it), any(l.status == "active" for l in it.listings),
                              0 if ql and it.title.lower().startswith(ql) else 1, -_sim(q, it.title)))
    return [_item_json(it) for it in rows[:limit]]


def suggest_items(db: Session, lst: Listing, limit: int = 6) -> list[dict]:
    exclude = lst.item_id
    cands = (db.query(Item).options(selectinload(Item.listings))
             .filter(Item.status.in_(IN_STOCK), Item.id != (exclude or 0)).limit(3000).all())
    cands = [it for it in cands if not is_placeholder(it) and not any(l.status == "active" for l in it.listings)]
    scored = sorted(((_sim(lst.title, it.title), it) for it in cands), key=lambda x: -x[0])
    return [_item_json(it) for s, it in scored[:limit] if s >= 0.2]
