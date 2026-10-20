"""End & relist, repricing and delayed promotion."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from .. import settings
from ..models import AutomationLog, Item, Listing
from ..util import utcnow
from . import ebay, notify
from .sync import account, add_event, refresh_item_status


def _log(db: Session, action: str, status: str, msg: str, lst: Listing | None = None,
         new_id: str | None = None) -> None:
    db.add(AutomationLog(action=action, status=status, message=msg,
                         ebay_item_id=lst.ebay_item_id if lst else None,
                         item_id=lst.item_id if lst else None, new_ebay_item_id=new_id))
    db.commit()


def reprice_target(item: Item | None, lst: Listing) -> float | None:
    """New price for the next relist, or None if price should stay the same."""
    if not settings.get("REPRICE_ENABLED") or lst.price is None:
        return None
    if item is not None and not item.reprice_enabled:
        return None
    listed = (item.listed_at if item else None) or lst.start_time
    if listed and (utcnow() - listed).days < int(settings.get("REPRICE_AFTER_DAYS") or 0):
        return None
    pct = float(settings.get("REPRICE_PERCENT") or 0)
    if pct <= 0:
        return None
    original = (item.original_price if item else None) or lst.price
    floor = original * (1 - float(settings.get("REPRICE_MAX_REDUCTION_PERCENT") or 0) / 100)
    if item is not None and item.min_price:
        floor = max(floor, item.min_price)
    new = round(lst.price * (1 - pct / 100), 2)
    new = round(max(new, floor), 2)
    if new >= lst.price - 0.005:
        return None
    return new


def relist_candidates(db: Session) -> list[tuple[Listing, str]]:
    """Listings due for end & relist, with the reason they qualify or were skipped."""
    now = utcnow()
    window = timedelta(hours=float(settings.get("RELIST_WINDOW_HOURS") or 1))
    min_age = int(settings.get("RELIST_MIN_AGE_DAYS") or 0)
    skip_watch = int(settings.get("RELIST_SKIP_WATCHERS") or 0)
    out = []
    q = db.query(Listing).filter(Listing.status == "active")
    for lst in q.all():
        if (lst.listing_type or "") != "FixedPriceItem" or (lst.duration or "") != "GTC":
            continue
        if not lst.end_time or lst.end_time - now > window or lst.end_time < now - timedelta(hours=2):
            continue
        reason = "due"
        if lst.item and not lst.item.relist_enabled:
            reason = "skipped: relisting turned off for this item"
        elif lst.start_time and (now - lst.start_time).days < max(min_age - 1, 0):
            reason = f"skipped: younger than {min_age} days"
        elif skip_watch and (lst.watch_count or 0) >= skip_watch:
            reason = f"skipped: {lst.watch_count} watchers"
        elif settings.get("RELIST_SKIP_WITH_SALES") and (lst.quantity_sold or 0) > 0:
            reason = "skipped: listing has sales"
        out.append((lst, reason))
    return out


def run_relist(db: Session) -> str:
    if not settings.get("RELIST_ENABLED"):
        return "relist disabled"
    acct = account(db)
    if not acct:
        return "eBay not connected"
    dry = settings.get("AUTOMATION_DRY_RUN")
    max_n = int(settings.get("RELIST_MAX_PER_RUN") or 50)
    done = errors = 0

    # 1) retry listings we ended but failed to relist (never leave an item offline)
    for lst in db.query(Listing).filter(Listing.pending_relist.is_(True)).all():
        _relist_one(db, acct, lst, lst.pending_relist_price, retry=True)

    # 2) new candidates
    for lst, reason in relist_candidates(db):
        if done >= max_n:
            break
        if reason != "due":
            continue
        price = reprice_target(lst.item, lst)
        if dry:
            msg = f"Would end & relist “{lst.title}”"
            if price:
                msg += f" and lower price {lst.price:.2f} → {price:.2f}"
            _log(db, "relist", "dry_run", msg, lst)
            done += 1
            continue
        try:
            ebay.trading(db, acct, "EndFixedPriceItem",
                         f"<ItemID>{lst.ebay_item_id}</ItemID><EndingReason>NotAvailable</EndingReason>")
        except ebay.EbayError as exc:
            errors += 1
            _log(db, "relist", "error", f"End failed: {exc}", lst)
            continue
        lst.pending_relist = True
        lst.pending_relist_price = price
        lst.end_time = utcnow()
        db.commit()
        if _relist_one(db, acct, lst, price):
            done += 1
        else:
            errors += 1
    return f"{done} relisted{' (dry run)' if dry else ''}, {errors} errors"


def _relist_one(db: Session, acct, lst: Listing, price: float | None, retry: bool = False) -> bool:
    price_xml = f"<StartPrice>{price:.2f}</StartPrice>" if price else ""
    try:
        try:
            root = ebay.trading(db, acct, "RelistFixedPriceItem",
                                f"<Item><ItemID>{lst.ebay_item_id}</ItemID>{price_xml}</Item>")
        except ebay.EbayError:
            if not price_xml:
                raise
            # e.g. variation listings can't take an item-level price; relist as-is.
            root = ebay.trading(db, acct, "RelistFixedPriceItem", f"<Item><ItemID>{lst.ebay_item_id}</ItemID></Item>")
            price = None
    except ebay.EbayError as exc:
        _log(db, "relist", "error", f"{'Retry r' if retry else 'R'}elist failed — item is currently ENDED on eBay: {exc}", lst)
        if not retry:
            notify.send(db, "issue", f"⚠️ Relist failed: {lst.title}",
                        f"The listing was ended but could not be relisted. FlipDesk will retry next run.\n{exc}",
                        f"/listings?q={lst.ebay_item_id}")
        return False
    new_id = ebay.text(root, "ItemID")
    now = utcnow()
    old_price = lst.price
    lst.status = "relisted"
    lst.relisted_as = new_id
    lst.pending_relist = False
    lst.pending_relist_price = None
    new = Listing(ebay_item_id=new_id, item_id=lst.item_id, title=lst.title, sku=lst.sku,
                  price=price or lst.price, currency=lst.currency, quantity=lst.quantity, quantity_sold=0,
                  listing_type=lst.listing_type, duration=lst.duration, start_time=now,
                  end_time=now + timedelta(days=30), status="active", image_url=lst.image_url,
                  url=f"https://www.ebay.com/itm/{new_id}", previous_ebay_item_id=lst.ebay_item_id, last_seen=now)
    db.add(new)
    db.flush()
    if lst.item:
        lst.item.current_ebay_item_id = new_id
        add_event(db, lst.item, "relist", f"Ended {lst.ebay_item_id} and relisted as {new_id}", new_id)
        if price:
            add_event(db, lst.item, "price", f"Repriced {old_price:.2f} → {price:.2f}", new_id)
        refresh_item_status(db, lst.item)
    db.commit()
    msg = f"Relisted “{lst.title}” as {new_id}" + (f", price {old_price:.2f} → {price:.2f}" if price else "")
    _log(db, "relist", "ok", msg, lst, new_id)
    return True


# ---------------------------------------------------------------- promotion
def promote_candidates(db: Session) -> list[Listing]:
    days = int(settings.get("PROMOTE_AFTER_DAYS"))
    cutoff = utcnow() - timedelta(days=days)
    out = []
    for lst in db.query(Listing).filter(Listing.status == "active", Listing.promoted.is_(False)).all():
        item = lst.item
        if item is not None and not item.promote_enabled:
            continue
        listed = (item.listed_at if item else None) or lst.start_time
        if listed and listed <= cutoff:
            out.append(lst)
    return out


def run_promote(db: Session) -> str:
    if not settings.get("PROMOTE_ENABLED"):
        return "promotion disabled"
    acct = account(db)
    if not acct:
        return "eBay not connected"
    cid = settings.get("PROMOTE_CAMPAIGN_ID")
    if not cid:
        return "no campaign configured"
    rate = f"{float(settings.get('PROMOTE_AD_RATE') or 2.0):.1f}"
    dry = settings.get("AUTOMATION_DRY_RUN")
    n = err = 0
    for lst in promote_candidates(db)[:500]:
        if dry:
            _log(db, "promote", "dry_run", f"Would promote “{lst.title}” at {rate}%", lst)
            n += 1
            continue
        try:
            ebay.rest(db, acct, "POST", f"/sell/marketing/v1/ad_campaign/{cid}/ad",
                      json={"listingId": lst.ebay_item_id, "bidPercentage": rate})
            ok = True
        except ebay.EbayError as exc:
            ok = exc.status == 409 or "already" in str(exc).lower()
            if not ok:
                err += 1
                _log(db, "promote", "error", f"Promote failed: {exc}", lst)
        if ok:
            lst.promoted = True
            lst.promoted_at = utcnow()
            if lst.item:
                add_event(db, lst.item, "promoted", f"Added to campaign {cid} at {rate}%", lst.ebay_item_id)
            db.commit()
            _log(db, "promote", "ok", f"Promoted “{lst.title}” at {rate}%", lst)
            n += 1
    return f"{n} promoted{' (dry run)' if dry else ''}, {err} errors"


def list_campaigns(db: Session) -> list[dict]:
    acct = account(db)
    if not acct:
        return []
    data = ebay.rest(db, acct, "GET", "/sell/marketing/v1/ad_campaign", params={"limit": 100})
    return [{"id": c.get("campaignId"), "name": c.get("campaignName"), "status": c.get("campaignStatus"),
             "strategy": (c.get("fundingStrategy") or {}).get("fundingModel"),
             "rate": (c.get("fundingStrategy") or {}).get("bidPercentage")}
            for c in data.get("campaigns", []) or []]
