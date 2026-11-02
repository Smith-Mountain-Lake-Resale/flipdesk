"""Promoted Listings ad fees (and other separately-billed charges) end up on the right sale.

eBay bills ad fees as NON_SALE_CHARGE / AD_FEE transactions that usually have no top-level
`orderId`; the order (or just the item) is named in `references`.

Run:  python tests/test_adfees.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import timedelta

if "DATA_DIR" not in os.environ:
    _t = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _t + "/data"
    os.environ["CONFIG_DIR"] = _t + "/config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import SessionLocal, init_db  # noqa: E402


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main():
    init_db()
    from app.models import Expense, FinanceTransaction, Sale
    from app.services import sync
    from app.util import utcnow

    now = utcnow()
    db = SessionLocal()

    def sale(oid, item, price, days, line="1"):
        s = Sale(marketplace="eBay", order_id=oid, line_item_id=line, ebay_item_id=item, sale_date=now - timedelta(days=days),
                 sale_price=price, status="paid")
        db.add(s)
        return s

    sale("11-00001-00001", "110000000001", 40.0, 5)                        # ad fee with ORDER_ID reference
    sale("11-00002-00002", "110000000002", 25.0, 4)                        # ad fee with only ITEM_ID reference
    sale("11-00003-00003", "110000000003", 30.0, 30)                       # same item sold twice (multi-qty):
    sale("11-00004-00004", "110000000003", 30.0, 2)                        #   fee goes to the sale it followed
    sale("11-00005-00005", "110000000005", 60.0, 3, line="A")              # two-line order — fee split by price
    sale("11-00005-00005", "110000000006", 20.0, 3, line="B")
    sale("11-00006-00006", "110000000007", 50.0, 6)                        # legacy <item>-<txn> order ref
    db.commit()

    def txn(tid, refs=None, amount="1.00", fee="AD_FEE", days=1, order=None, ttype="NON_SALE_CHARGE", entry="DEBIT"):
        t = {"transactionId": tid, "transactionType": ttype, "bookingEntry": entry, "feeType": fee,
             "amount": {"value": amount, "currency": "USD"}, "transactionDate": iso(now - timedelta(days=days)),
             "references": [{"referenceId": i, "referenceType": k} for k, i in (refs or [])]}
        if order:
            t["orderId"] = order
        return t

    txns = [
        txn("A1", [("ORDER_ID", "11-00001-00001"), ("ITEM_ID", "110000000001")], "3.20", days=5),
        txn("A2", [("ITEM_ID", "110000000002")], "1.75", days=4),
        txn("A3", [("ITEM_ID", "110000000003")], "2.10", days=2),
        txn("A4", [("ORDER_ID", "11-00005-00005")], "4.00", days=3),
        txn("A5", [("ORDER_ID", "110000000007-2900000000001")], "2.50", days=6),
        txn("A6", [("ORDER_ID", "11-00001-00001")], "0.70", fee="AD_FEE", days=1, ttype="CREDIT", entry="CREDIT"),  # partial ad credit
        txn("N1", [("ORDER_ID", "99-NOT-SYNCED")], "1.11", days=2),       # order we don't have → expensed
        txn("N2", [], "21.95", fee="SUBSCRIPTION_FEE", days=9),            # store subscription → expensed
        txn("N3", [("ITEM_ID", "110000000999")], "0.35", fee="INSERTION_FEE", days=9),  # unsold item → expensed
    ]
    for t in txns:
        sync._upsert_txn(db, t)
    db.commit()
    for oid in {r.order_id for r in db.query(FinanceTransaction) if r.order_id}:
        sync.apply_finances_to_order(db, oid)
    touched = sync.attribute_charges(db)
    for oid in touched:
        sync.apply_finances_to_order(db, oid)
    sync._non_order_charges_to_ledger(db)
    db.commit()

    ad = {(s.order_id, s.line_item_id): s.ad_fees for s in db.query(Sale)}
    assert ad[("11-00001-00001", "1")] == 2.50, ad          # 3.20 fee − 0.70 credit
    assert ad[("11-00002-00002", "1")] == 1.75, ad
    assert ad[("11-00003-00003", "1")] == 0 and ad[("11-00004-00004", "1")] == 2.10, ad
    assert ad[("11-00005-00005", "A")] == 3.00 and ad[("11-00005-00005", "B")] == 1.00, ad
    assert ad[("11-00006-00006", "1")] == 2.50, ad
    print("✓ ad fees attached via ORDER_ID, ITEM_ID (latest prior sale), legacy order IDs; split across lines; credits net off")

    exp = {e.external_ref: e for e in db.query(Expense).filter_by(source="ebay_sync")}
    assert set(exp) == {"ebay:NON_SALE_CHARGE:N1", "ebay:NON_SALE_CHARGE:N2", "ebay:NON_SALE_CHARGE:N3"}, set(exp)
    assert exp["ebay:NON_SALE_CHARGE:N1"].category == "Advertising"
    assert exp["ebay:NON_SALE_CHARGE:N2"].category == "Software and subscriptions"
    print("✓ only charges with no synced order go to expenses (no double counting)")

    # ---- the missing order syncs later → its fee moves from expenses onto the sale
    sale("99-NOT-SYNCED", "110000000888", 15.0, 2)
    db.commit()
    for oid in sync.attribute_charges(db):
        sync.apply_finances_to_order(db, oid)
    db.commit()
    assert db.query(Sale).filter_by(order_id="99-NOT-SYNCED").one().ad_fees == 1.11
    assert not db.query(Expense).filter_by(external_ref="ebay:NON_SALE_CHARGE:N1").first()
    print("✓ fee expensed while its order was missing moves onto the sale once the order syncs")

    # ---- data from older versions: ad fee stored with no order + an Advertising expense
    s = sale("11-00007-00007", "110000000010", 35.0, 8)
    raw = txn("OLD1", [("ORDER_ID", "11-00007-00007")], "2.80", days=8)
    db.add(FinanceTransaction(transaction_id="OLD1", transaction_type="NON_SALE_CHARGE", booking_entry="DEBIT",
                              order_id=None, amount=2.80, fee_type="AD_FEE", transaction_date=now - timedelta(days=8),
                              raw=json.dumps(raw)))
    db.add(Expense(expense_date=(now - timedelta(days=8)).date(), category="Advertising", vendor="eBay",
                   description="Ad Fee", amount=2.80, source="ebay_sync", external_ref="ebay:NON_SALE_CHARGE:OLD1"))
    manual = Expense(expense_date=now.date(), category="Advertising", vendor="eBay", description="my own", amount=5,
                     source="manual")
    db.add(manual)
    db.commit()
    n = sync.repair_charge_attribution(db)
    assert n == 1
    assert db.query(Sale).filter_by(order_id="11-00007-00007").one().ad_fees == 2.80
    assert not db.query(Expense).filter_by(external_ref="ebay:NON_SALE_CHARGE:OLD1").first()
    assert db.query(Expense).filter_by(description="my own").first()          # manual expenses untouched
    assert sync.repair_charge_attribution(db) == 0                              # idempotent
    print("✓ startup repair moves previously-expensed ad fees onto their sales (once; manual expenses untouched)")
    db.close()
    print("ADFEES ALL GOOD")


if __name__ == "__main__":
    main()
