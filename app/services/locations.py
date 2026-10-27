"""Shelf/bin locations: codes, item linking, QR content and scan parsing."""
from __future__ import annotations

import re

from sqlalchemy import event, func
from sqlalchemy.orm import Session

from .. import settings
from ..models import Item, ItemEvent, Location
from ..util import utcnow

SCAN_URL_RE = re.compile(r"/l/([A-Za-z0-9_-]{1,20})(?:[/?#]|$)", re.IGNORECASE)
CODE_RE = re.compile(r"^[A-Za-z]{0,3}\d{1,8}$|^L[A-Za-z0-9_-]{1,19}$")


def _next_code(db: Session) -> str:
    with db.no_autoflush:
        n = (db.query(func.max(Location.id)).scalar() or 0) + 1
        pending = sum(1 for o in db.new if isinstance(o, Location))
        n += pending
        while True:
            code = f"L{n:04d}"
            if not db.query(Location.id).filter(Location.code == code).first() and \
                    not any(isinstance(o, Location) and o.code == code for o in db.new):
                return code
            n += 1


def find_by_name(db: Session, name: str) -> Location | None:
    with db.no_autoflush:
        loc = db.query(Location).filter(func.lower(Location.name) == name.strip().lower()).first()
        if loc:
            return loc
        for o in db.new:  # created earlier in this same flush
            if isinstance(o, Location) and o.name.lower() == name.strip().lower():
                return o
    return None


def ensure(db: Session, name: str) -> Location:
    name = name.strip()[:255]
    loc = find_by_name(db, name)
    if loc is None:
        loc = Location(name=name, code=_next_code(db))
        db.add(loc)
    return loc


def by_code(db: Session, code: str) -> Location | None:
    return db.query(Location).filter(func.upper(Location.code) == code.strip().upper()).first()


def set_item_location(db: Session, item: Item, loc: Location | None, note: str = "") -> bool:
    """Move an item. Returns True if anything changed."""
    old = item.storage_location
    new = loc.name if loc else None
    if item.location_id == (loc.id if loc else None) and old == new:
        return False
    item.location = loc
    item.location_id = loc.id if loc else None
    item.storage_location = new
    db.add(ItemEvent(item_id=item.id, kind="location",
                     detail=f"Location {old or '—'} → {new or '—'}{(' (' + note + ')') if note else ''}", ts=utcnow()))
    return True


def rename(db: Session, loc: Location, name: str) -> None:
    loc.name = name.strip()[:255]
    for it in loc.items:
        it.storage_location = loc.name


def backfill(db: Session) -> int:
    """Turn existing free-text locations into Location rows (idempotent)."""
    n = 0
    for it in db.query(Item).filter(Item.storage_location.isnot(None), Item.location_id.is_(None)).all():
        if it.storage_location.strip():
            loc = ensure(db, it.storage_location)
            db.flush()
            it.location_id = loc.id
            it.storage_location = loc.name
            n += 1
    db.commit()
    return n


def public_base(request) -> str:
    base = (settings.get("PUBLIC_URL") or "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


def qr_url(base: str, code: str) -> str:
    return f"{base}/l/{code}"


def parse_scan(text: str) -> str | None:
    """Accept a label URL (…/l/L0007) or a bare code (L0007)."""
    text = (text or "").strip()
    m = SCAN_URL_RE.search(text)
    if m:
        return m.group(1).upper()
    if CODE_RE.match(text):
        return text.upper()
    return None


# Keep item.location_id in sync whenever storage_location text is edited anywhere
# (item form, quick edit, CSV import, …). Unknown names create a new Location.
@event.listens_for(Session, "before_flush")
def _sync_locations(session: Session, flush_context, instances) -> None:
    from sqlalchemy import inspect as sa_inspect
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Item):
            continue
        state = sa_inspect(obj)
        hist = state.attrs.storage_location.history
        if obj in session.new or hist.has_changes():
            text = (obj.storage_location or "").strip()
            if not text:
                obj.location = None
                obj.location_id = None
                continue
            if obj.location is not None and obj.location.name.lower() == text.lower():
                continue
            loc = ensure(session, text)
            obj.location = loc
            obj.storage_location = loc.name
