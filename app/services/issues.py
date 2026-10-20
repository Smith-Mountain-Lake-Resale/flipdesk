"""Error detection: things that otherwise slip by silently."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlalchemy.orm import Session

from .. import settings
from ..models import AutomationLog, EbayAccount, FinanceTransaction, Listing, Notification
from ..util import utcnow
from . import notify


def detect(db: Session) -> list[dict]:
    issues: list[dict] = []
    labels: dict[str, list[FinanceTransaction]] = defaultdict(list)
    for t in db.query(FinanceTransaction).filter(FinanceTransaction.transaction_type == "SHIPPING_LABEL").all():
        if t.order_id:
            labels[t.order_id].append(t)
    for oid, ts in labels.items():
        debits = [t for t in ts if t.booking_entry != "CREDIT"]
        credits = [t for t in ts if t.booking_entry == "CREDIT"]
        if len(debits) - len(credits) >= 2:
            issues.append({
                "key": f"dup_label:{oid}", "severity": "high", "kind": "Duplicate shipping label",
                "title": f"Order {oid} was charged for {len(debits)} shipping labels",
                "detail": "Total " + ", ".join(f"{t.amount:.2f}" for t in debits) + ". If you only shipped once, void the extra label in Seller Hub for a refund.",
                "date": max(t.transaction_date for t in debits), "link": f"/sales?q={oid}"})
        adj = [t for t in ts if t.memo and "adjust" in t.memo.lower()]
        for t in adj:
            issues.append({
                "key": f"label_adj:{t.transaction_id}", "severity": "medium", "kind": "Label cost adjustment",
                "title": f"Shipping label adjustment of {t.amount:.2f} on order {oid}",
                "detail": t.memo, "date": t.transaction_date, "link": f"/sales?q={oid}"})
    for t in db.query(FinanceTransaction).filter(FinanceTransaction.transaction_type == "ADJUSTMENT").all():
        issues.append({
            "key": f"adj:{t.transaction_id}", "severity": "medium", "kind": "Account adjustment",
            "title": f"eBay {'credited' if t.booking_entry == 'CREDIT' else 'charged'} {t.amount:.2f}",
            "detail": t.memo or t.fee_type or "", "date": t.transaction_date,
            "link": f"/sales?q={t.order_id}" if t.order_id else None})
    for t in db.query(FinanceTransaction).filter(FinanceTransaction.transaction_type == "CREDIT").all():
        issues.append({
            "key": f"credit:{t.transaction_id}", "severity": "info", "kind": "Fee refund / credit",
            "title": f"eBay credited {t.amount:.2f}" + (f" on order {t.order_id}" if t.order_id else ""),
            "detail": t.memo or (t.fee_type or "").replace("_", " ").title(), "date": t.transaction_date,
            "link": f"/sales?q={t.order_id}" if t.order_id else None})
    for lst in db.query(Listing).filter(Listing.pending_relist.is_(True)).all():
        issues.append({
            "key": f"pending_relist:{lst.ebay_item_id}", "severity": "high", "kind": "Failed relist",
            "title": f"“{lst.title}” was ended but not relisted",
            "detail": "FlipDesk retries automatically each run. You can also relist it from Seller Hub.",
            "date": lst.end_time or utcnow(), "link": f"/listings?q={lst.ebay_item_id}"})
    since = utcnow() - timedelta(days=14)
    for log in db.query(AutomationLog).filter(AutomationLog.status == "error", AutomationLog.ts >= since).all():
        issues.append({
            "key": f"auto_err:{log.id}", "severity": "medium", "kind": f"Automation error ({log.action})",
            "title": log.message[:160] if log.message else "Automation error", "detail": log.message,
            "date": log.ts, "link": "/automations"})
    acct = db.query(EbayAccount).first()
    if acct and acct.last_error:
        issues.append({"key": "sync_error", "severity": "medium", "kind": "Sync error",
                       "title": "Last eBay sync reported an error", "detail": acct.last_error,
                       "date": utcnow(), "link": "/settings/ebay"})
    if acct and acct.refresh_expires and acct.refresh_expires - utcnow() < timedelta(days=30):
        issues.append({"key": "token_expiring", "severity": "high", "kind": "eBay authorization",
                       "title": "eBay authorization expires soon — reconnect your account",
                       "detail": f"Refresh token expires {acct.refresh_expires:%Y-%m-%d}.", "date": utcnow(),
                       "link": "/settings/ebay"})
    order = {"high": 0, "medium": 1, "info": 2, "low": 3}
    issues.sort(key=lambda i: (order.get(i["severity"], 9), -(i["date"].timestamp() if i["date"] else 0)))
    return issues


def alert_new(db: Session) -> int:
    """Push alerts for new high/medium issues (deduplicated by key)."""
    if not settings.get("ALERT_ON_ISSUES"):
        return 0
    n = 0
    for i in detect(db):
        if i["severity"] not in ("high", "medium") or i["key"] in ("sync_error",):
            continue
        if i["date"] and i["date"] < utcnow() - timedelta(days=3):
            continue  # don't alert on history pulled in by the backfill
        marker = f"[{i['key']}]"
        if db.query(Notification.id).filter(Notification.body.contains(marker)).first():
            continue
        notify.send(db, "issue", f"⚠️ {i['kind']}: {i['title']}", f"{i['detail'] or ''}\n{marker}", i.get("link"))
        n += 1
    return n
