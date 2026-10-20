"""Pull data from eBay: active listings, orders (2-year backfill) and finances."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import settings
from ..models import (EbayAccount, Expense, FinanceTransaction, Item, ItemEvent,
                      Listing, Sale)
from ..util import parse_iso, to_float, utcnow
from . import ebay, notify
from .ebay import NS, text

ISO_DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def _dur(s: str | None) -> timedelta | None:
    if not s:
        return None
    m = ISO_DUR.fullmatch(s.strip())
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=se)


def account(db: Session) -> EbayAccount | None:
    return db.query(EbayAccount).first()


def add_event(db: Session, item: Item, kind: str, detail: str, ebay_item_id: str | None = None,
              ts: datetime | None = None) -> None:
    db.add(ItemEvent(item_id=item.id, kind=kind, detail=detail, ebay_item_id=ebay_item_id, ts=ts or utcnow()))


# ======================================================================== items
def refresh_item_status(db: Session, item: Item) -> None:
    if item.status == "written_off":
        return
    sales = [s for s in item.sales if s.status != "cancelled" and not s.restocked]
    sold = sum(s.quantity or 1 for s in sales)
    item.quantity_sold = sold
    has_active = any(lst.status == "active" or lst.pending_relist for lst in item.listings)
    starts = [lst.start_time for lst in item.listings if lst.start_time]
    if starts and (item.listed_at is None or min(starts) < item.listed_at):
        item.listed_at = min(starts)
    if sold >= (item.quantity or 1) and sold > 0:
        item.status = "sold"
        item.sold_at = max(s.sale_date for s in sales)
    elif has_active:
        item.status = "listed"
        item.sold_at = None
    else:
        item.status = "unlisted"
        item.sold_at = None


def item_for_listing(db: Session, listing: Listing, quantity: int | None = None) -> Item:
    if listing.item_id:
        return listing.item
    item = None
    if listing.previous_ebay_item_id:
        prev = db.query(Listing).filter_by(ebay_item_id=listing.previous_ebay_item_id).first()
        if prev and prev.item_id:
            item = prev.item
    if item is None and listing.sku:
        item = (db.query(Item).filter(Item.sku == listing.sku, Item.status.in_(("unlisted", "listed")))
                .order_by(Item.id).first()) or db.query(Item).filter(Item.sku == listing.sku).order_by(Item.id.desc()).first()
    if item is None:
        item = Item(title=listing.title or f"eBay item {listing.ebay_item_id}", sku=listing.sku,
                    status="listed", quantity=max(quantity or 1, 1), image_url=listing.image_url,
                    original_price=listing.price, listed_at=listing.start_time)
        db.add(item)
        db.flush()
        add_event(db, item, "created", "Imported from eBay listing", listing.ebay_item_id, listing.start_time)
    listing.item_id = item.id
    listing.item = item
    if item.original_price is None:
        item.original_price = listing.price
    if not item.image_url and listing.image_url:
        item.image_url = listing.image_url
    return item


# ======================================================================== listings
def sync_listings(db: Session, acct: EbayAccount) -> str:
    seen: set[str] = set()
    page, pages, count = 1, 1, 0
    now = utcnow()
    while page <= pages:
        root = ebay.trading(db, acct, "GetMyeBaySelling", (
            "<ActiveList><Include>true</Include><Pagination>"
            f"<EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber>"
            "</Pagination></ActiveList><DetailLevel>ReturnAll</DetailLevel>"))
        active = root.find(NS + "ActiveList")
        if active is None:
            break
        pages = int(text(active, "PaginationResult/TotalNumberOfPages", "1") or 1)
        arr = active.find(NS + "ItemArray")
        for el in (arr if arr is not None else []):
            _upsert_listing(db, el, now)
            seen.add(text(el, "ItemID"))
            count += 1
        db.commit()
        page += 1

    # Anything we thought was active but eBay no longer reports has ended / sold.
    for lst in db.query(Listing).filter(Listing.status == "active").all():
        if lst.ebay_item_id in seen:
            continue
        if lst.relisted_as:
            lst.status = "relisted"
        elif lst.pending_relist:
            continue
        else:
            lst.status = "sold" if (lst.quantity_sold or 0) >= (lst.quantity or 1) else "ended"
            lst.end_time = lst.end_time or now
        if lst.item:
            refresh_item_status(db, lst.item)
    acct.listings_synced_at = now
    db.commit()
    return f"{count} active listings"


def _upsert_listing(db: Session, el, now: datetime) -> None:
    item_id = text(el, "ItemID")
    lst = db.query(Listing).filter_by(ebay_item_id=item_id).first()
    is_new = lst is None
    if is_new:
        lst = Listing(ebay_item_id=item_id)
        db.add(lst)
    price_el = el.find(f"{NS}SellingStatus/{NS}CurrentPrice")
    if price_el is None:
        price_el = el.find(NS + "BuyItNowPrice")
    new_price = to_float(price_el.text if price_el is not None else None, None)
    new_title = text(el, "Title")
    qty = int(text(el, "Quantity", "1") or 1)
    avail = int(text(el, "QuantityAvailable", str(qty)) or qty)
    old_price, old_title = lst.price, lst.title

    lst.title = new_title
    lst.sku = text(el, "SKU")
    lst.price = new_price
    lst.currency = price_el.get("currencyID") if price_el is not None else None
    lst.quantity = qty
    lst.quantity_sold = max(qty - avail, 0)
    lst.listing_type = text(el, "ListingType")
    lst.duration = text(el, "ListingDuration")
    lst.start_time = parse_iso(text(el, "ListingDetails/StartTime")) or lst.start_time
    left = _dur(text(el, "TimeLeft"))
    lst.end_time = (now + left) if left else parse_iso(text(el, "ListingDetails/EndTime"))
    lst.watch_count = int(text(el, "WatchCount", "0") or 0)
    lst.image_url = text(el, "PictureDetails/GalleryURL") or lst.image_url
    lst.url = text(el, "ListingDetails/ViewItemURL") or lst.url
    lst.status = "active"
    lst.pending_relist = False
    lst.last_seen = now
    db.flush()

    item = item_for_listing(db, lst, qty)
    item.current_ebay_item_id = item_id
    if item.quantity < qty and item.status != "sold":
        item.quantity = qty
    if not is_new and old_price is not None and new_price is not None and abs(old_price - new_price) > 0.001:
        add_event(db, item, "price", f"Price changed {old_price:.2f} → {new_price:.2f}", item_id)
    if not is_new and old_title and new_title and old_title != new_title:
        add_event(db, item, "title", f"Title changed: “{old_title}” → “{new_title}”", item_id)
    if is_new and not lst.previous_ebay_item_id:
        add_event(db, item, "listed", f"Listed on eBay at {lst.price or 0:.2f}", item_id, lst.start_time)
    refresh_item_status(db, item)


# ======================================================================== orders
def sync_orders(db: Session, acct: EbayAccount) -> str:
    now = utcnow()
    filters: list[str] = []
    backfill = not acct.backfill_done
    if backfill:
        days = min(int(settings.get("BACKFILL_DAYS") or 730), 729)
        start = now - timedelta(days=days)
        while start < now:
            end = min(start + timedelta(days=90), now)
            filters.append(f"creationdate:[{_z(start)}..{_z(end)}]")
            start = end
    else:
        since = (acct.orders_synced_at or now - timedelta(days=7)) - timedelta(hours=6)
        filters.append(f"lastmodifieddate:[{_z(since)}..]")

    new_sales, total = [], 0
    for f in filters:
        offset = 0
        while True:
            data = ebay.rest(db, acct, "GET", "/sell/fulfillment/v1/order",
                             params={"filter": f, "limit": 200, "offset": offset})
            orders = data.get("orders", [])
            for o in orders:
                new_sales.extend(_upsert_order(db, o, alert=not backfill))
                total += 1
            db.commit()
            offset += len(orders)
            if not orders or offset >= int(data.get("total", 0)):
                break
    acct.orders_synced_at = now
    if backfill:
        acct.backfill_done = True
    db.commit()
    for s in new_sales:
        _alert_sale(db, s)
    return f"{total} orders processed, {len(new_sales)} new sales"


def _z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _amt(d: dict | None, key: str = "value") -> float:
    if not d:
        return 0.0
    return to_float(d.get(key), 0.0) or 0.0


def _upsert_order(db: Session, o: dict, alert: bool) -> list[Sale]:
    order_id = o["orderId"]
    created = parse_iso(o.get("creationDate")) or utcnow()
    pay_status = o.get("orderPaymentStatus")
    cancel_state = (o.get("cancelStatus") or {}).get("cancelState")
    store_buyer = settings.get("STORE_BUYER_USERNAME")
    ship_to = {}
    try:
        ship_to = o["fulfillmentStartInstructions"][0]["shippingStep"]["shipTo"]["contactAddress"]
    except (KeyError, IndexError, TypeError):
        pass
    lines = o.get("lineItems", [])
    subtotal_all = sum(_amt(li.get("lineItemCost")) for li in lines) or 1.0
    order_fee = _amt(o.get("totalMarketplaceFee"))
    order_refunds = [r for r in (o.get("paymentSummary") or {}).get("refunds", []) or []
                     if r.get("refundStatus", "REFUNDED") in ("REFUNDED", "SUCCESS", None)]
    order_refund_total = sum(_amt(r.get("amount")) for r in order_refunds)

    created_sales = []
    for li in lines:
        line_id = str(li.get("lineItemId"))
        sale = db.query(Sale).filter_by(marketplace="eBay", order_id=order_id, line_item_id=line_id).first()
        is_new = sale is None
        if is_new:
            sale = Sale(marketplace="eBay", order_id=order_id, line_item_id=line_id, source="ebay_sync")
            db.add(sale)
        cost = _amt(li.get("lineItemCost"))
        discount = sum(_amt(p.get("discountAmount")) for p in li.get("appliedPromotions", []) or [])
        share = cost / subtotal_all
        sale.ebay_item_id = li.get("legacyItemId")
        sale.title = li.get("title")
        sale.sku = li.get("sku")
        sale.quantity = int(li.get("quantity") or 1)
        sale.sale_date = created
        sale.sale_price = round(cost - discount, 2)
        sale.shipping_charged = round(_amt((li.get("deliveryCost") or {}).get("shippingCost"))
                                      + _amt((li.get("deliveryCost") or {}).get("handlingCost")), 2)
        sale.tax_collected = round(sum(_amt(t.get("amount")) for t in li.get("ebayCollectAndRemitTaxes", []) or []), 2)
        if not sale.marketplace_fees or is_new or not _has_finance(db, order_id):
            sale.marketplace_fees = round(order_fee * share, 2)
        sale.fulfillment_status = o.get("orderFulfillmentStatus")
        sale.buyer_city = ship_to.get("city")
        sale.buyer_state = ship_to.get("stateOrProvince")
        sale.buyer_postal = (ship_to.get("postalCode") or "")[:5] or None
        sale.buyer_country = ship_to.get("countryCode")
        sale.buyer_username = (o.get("buyer") or {}).get("username") if store_buyer else None

        # ---- refunds & cancellations (auto-reconciliation)
        line_refunds = li.get("refunds", []) or []
        refund = sum(_amt(r.get("amount")) for r in line_refunds)
        refund_dates = [parse_iso(r.get("refundDate")) for r in line_refunds if r.get("refundDate")]
        if not line_refunds and order_refund_total:
            refund = order_refund_total * share
            refund_dates = [parse_iso(r.get("refundDate")) for r in order_refunds if r.get("refundDate")]
        gross = sale.sale_price + sale.shipping_charged
        prev_status = sale.status
        if cancel_state == "CANCELED":
            sale.status = "cancelled"
            sale.refund_amount = 0
        elif pay_status == "FULLY_REFUNDED":
            sale.status = "refunded"
            sale.refund_amount = round(gross, 2)
        elif refund > 0 or pay_status == "PARTIALLY_REFUNDED":
            sale.status = "partially_refunded"
            sale.refund_amount = round(min(refund, gross), 2)
        else:
            sale.status = "completed"
            sale.refund_amount = 0
        sale.refund_date = max([d for d in refund_dates if d], default=None) if sale.refund_amount else None
        db.flush()

        # ---- link to inventory
        item = sale.item
        if item is None:
            item = _item_for_sale(db, sale)
            sale.item_id = item.id
            sale.item = item
        if prev_status != sale.status and not is_new:
            add_event(db, item, "reconcile", f"Order {order_id}: {prev_status} → {sale.status}")
        if sale.status == "cancelled" and settings.get("RESTOCK_ON_CANCEL"):
            sale.restocked = True
        if sale.status == "refunded" and settings.get("RESTOCK_ON_FULL_REFUND") and prev_status != "refunded":
            sale.restocked = True
        if is_new:
            add_event(db, item, "sold", f"Sold on eBay for {sale.sale_price:.2f} (order {order_id})",
                      sale.ebay_item_id, created)
            sale.alerted = not alert
            if alert:
                created_sales.append(sale)
        db.flush()
        db.refresh(item)
        refresh_item_status(db, item)
    return created_sales


def _has_finance(db: Session, order_id: str) -> bool:
    return db.query(FinanceTransaction.id).filter_by(order_id=order_id, transaction_type="SALE").first() is not None


def _item_for_sale(db: Session, sale: Sale) -> Item:
    if sale.ebay_item_id:
        lst = db.query(Listing).filter_by(ebay_item_id=sale.ebay_item_id).first()
        if lst and lst.item_id:
            return lst.item
        prev = db.query(Item).filter_by(current_ebay_item_id=sale.ebay_item_id).first()
        if prev:
            return prev
    if sale.sku:
        it = db.query(Item).filter(Item.sku == sale.sku).order_by(Item.id.desc()).first()
        if it:
            return it
    item = Item(title=sale.title or f"eBay order {sale.order_id}", sku=sale.sku, status="sold",
                quantity=sale.quantity or 1, current_ebay_item_id=sale.ebay_item_id)
    db.add(item)
    db.flush()
    add_event(db, item, "created", "Imported from eBay order history", sale.ebay_item_id, sale.sale_date)
    return item


def _alert_sale(db: Session, sale: Sale) -> None:
    if not settings.get("ALERT_ON_SALE") or sale.alerted:
        return
    loc = sale.item.storage_location if sale.item else None
    body = f"{sale.title}\nSold for {sale.sale_price:.2f} + {sale.shipping_charged:.2f} shipping"
    body += f"\nLocation: {loc or 'not set'}"
    if sale.buyer_city or sale.buyer_state:
        body += f"\nShip to: {sale.buyer_city or ''}, {sale.buyer_state or ''} {sale.buyer_country or ''}"
    notify.send(db, "sale", f"💰 Sold: {sale.title[:80] if sale.title else sale.order_id}", body,
                f"/sales/{sale.id}")
    sale.alerted = True
    db.commit()


# ======================================================================== finances
AD_HINTS = ("AD_FEE", "PROMOT", "ADVERT")


def _is_ad(fee_type: str | None, memo: str | None) -> bool:
    s = f"{fee_type or ''} {memo or ''}".upper()
    return any(h in s for h in AD_HINTS)


def sync_finances(db: Session, acct: EbayAccount) -> str:
    now = utcnow()
    if not acct.finances_backfill_done:
        start = now - timedelta(days=min(int(settings.get("BACKFILL_DAYS") or 730), 1095))
    else:
        start = (acct.finances_synced_at or now - timedelta(days=7)) - timedelta(days=2)
    touched_orders: set[str] = set()
    seen: set[tuple] = set()
    win_start = start
    while win_start < now:
        win_end = min(win_start + timedelta(days=90), now)
        offset = 0
        while True:
            data = ebay.rest(db, acct, "GET", "/sell/finances/v1/transaction", host="apiz", params={
                "filter": f"transactionDate:[{_z(win_start)}..{_z(win_end)}]",
                "limit": 1000, "offset": offset})
            txns = data.get("transactions", []) or []
            for t in txns:
                oid = _upsert_txn(db, t)
                if oid:
                    touched_orders.add(oid)
                seen.add((t.get("transactionId"), t.get("transactionType")))
            db.commit()
            offset += len(txns)
            if not txns or offset >= int(data.get("total", 0)):
                break
        win_start = win_end
    for oid in touched_orders:
        apply_finances_to_order(db, oid)
    _non_order_charges_to_ledger(db)
    acct.finances_synced_at = now
    acct.finances_backfill_done = True
    db.commit()
    return f"{len(seen)} finance transactions, {len(touched_orders)} orders updated"


def _upsert_txn(db: Session, t: dict) -> str | None:
    tid, ttype = t.get("transactionId"), t.get("transactionType")
    if not tid or not ttype:
        return None
    row = db.query(FinanceTransaction).filter_by(transaction_id=tid, transaction_type=ttype).first()
    if row is None:
        row = FinanceTransaction(transaction_id=tid, transaction_type=ttype)
        db.add(row)
    row.booking_entry = t.get("bookingEntry")
    row.order_id = t.get("orderId")
    row.amount = _amt(t.get("amount"))
    row.currency = (t.get("amount") or {}).get("currency")
    row.fee_amount = _amt(t.get("totalFeeAmount"))
    row.fee_type = t.get("feeType")
    row.memo = t.get("transactionMemo")
    row.reference_ids = ",".join(f"{r.get('referenceType')}:{r.get('referenceId')}" for r in t.get("references", []) or [])
    row.transaction_date = parse_iso(t.get("transactionDate")) or utcnow()
    row.status = t.get("transactionStatus")
    row.raw = json.dumps(t)[:20000]
    db.flush()
    return row.order_id


def apply_finances_to_order(db: Session, order_id: str) -> None:
    sales = db.query(Sale).filter_by(marketplace="eBay", order_id=order_id).all()
    if not sales:
        return
    txns = db.query(FinanceTransaction).filter_by(order_id=order_id).all()
    sign = lambda t: -1 if t.booking_entry == "CREDIT" else 1  # noqa: E731  (debit = cost)
    fees_by_line: dict[str, float] = {}
    fees_total = 0.0
    ads = 0.0
    labels = 0.0
    other = 0.0
    have_sale_txn = False
    for t in txns:
        if t.transaction_type == "SALE":
            have_sale_txn = True
            fees_total += t.fee_amount
            try:
                raw = json.loads(t.raw or "{}")
                for li in raw.get("orderLineItems", []) or []:
                    f = sum(_amt(m.get("amount")) for m in li.get("marketplaceFees", []) or [])
                    fees_by_line[str(li.get("lineItemId"))] = fees_by_line.get(str(li.get("lineItemId")), 0) + f
            except ValueError:
                pass
        elif t.transaction_type == "REFUND":
            fees_total -= t.fee_amount  # fee credits on refunds
        elif t.transaction_type == "SHIPPING_LABEL":
            labels += sign(t) * t.amount
        elif t.transaction_type in ("NON_SALE_CHARGE", "CREDIT", "ADJUSTMENT"):
            if _is_ad(t.fee_type, t.memo):
                ads += sign(t) * t.amount
            elif t.transaction_type == "NON_SALE_CHARGE" or t.fee_type:
                fees_total += sign(t) * t.amount
            else:
                other += sign(t) * t.amount
    subtotal = sum(s.sale_price for s in sales) or 1.0
    line_fee_sum = sum(fees_by_line.values())
    for s in sales:
        share = s.sale_price / subtotal if subtotal else 1 / len(sales)
        if have_sale_txn or fees_total:
            if fees_by_line and s.line_item_id in fees_by_line:
                extra = fees_total - line_fee_sum
                s.marketplace_fees = round(fees_by_line[s.line_item_id] + extra * share, 2)
            else:
                s.marketplace_fees = round(fees_total * share, 2)
        s.ad_fees = round(ads * share, 2)
        s.shipping_cost = round(labels * share, 2)
        if other:
            s.other_fees = round(other * share, 2)
    db.flush()


def _non_order_charges_to_ledger(db: Session) -> None:
    """Store subscriptions, insertion fees, unattributed ad fees → expense ledger."""
    rows = db.query(FinanceTransaction).filter(
        FinanceTransaction.order_id.is_(None),
        FinanceTransaction.transaction_type.in_(("NON_SALE_CHARGE",))).all()
    for t in rows:
        ref = f"ebay:{t.transaction_type}:{t.transaction_id}"
        if db.query(Expense.id).filter_by(external_ref=ref).first():
            continue
        amt = t.amount * (-1 if t.booking_entry == "CREDIT" else 1)
        cat = "Advertising" if _is_ad(t.fee_type, t.memo) else "Commissions and fees"
        if t.fee_type and "SUBSCRIPTION" in t.fee_type.upper():
            cat = "Software and subscriptions"
        db.add(Expense(expense_date=t.transaction_date.date(), category=cat, vendor="eBay",
                       description=(t.fee_type or "eBay charge").replace("_", " ").title() + (f" — {t.memo}" if t.memo else ""),
                       amount=round(amt, 2), source="ebay_sync", external_ref=ref))
    db.flush()


# ======================================================================== orchestration
def run_full_sync(db: Session) -> str:
    acct = account(db)
    if not acct:
        return "eBay not connected"
    msgs = []
    for name, fn in (("listings", sync_listings), ("orders", sync_orders), ("finances", sync_finances)):
        try:
            msgs.append(f"{name}: {fn(db, acct)}")
        except Exception as exc:
            db.rollback()
            msgs.append(f"{name}: ERROR {exc}")
            acct = account(db)
            acct.last_error = f"{name}: {exc}"[:2000]
            db.commit()
    return " | ".join(msgs)


def counts(db: Session) -> dict:
    return {
        "listings": db.query(func.count(Listing.id)).filter(Listing.status == "active").scalar(),
        "sales": db.query(func.count(Sale.id)).scalar(),
        "finance": db.query(func.count(FinanceTransaction.id)).scalar(),
    }
