"""End-to-end smoke test with a mocked eBay API.

Run:  DATA_DIR=/tmp/fd/data CONFIG_DIR=/tmp/fd/config python -m pytest -q tests/  (or python tests/test_app.py)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

if "DATA_DIR" not in os.environ:
    _t = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _t + "/data"
    os.environ["CONFIG_DIR"] = _t + "/config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import ebay  # noqa: E402

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ----------------------------------------------------------------- fake eBay
STATE = {"active": ["110000000001", "110000000002"], "relisted": {},
         "meta": {"110000000001": ("Vintage Pyrex Bowl", "PYX-1", 40.0, timedelta(days=29, hours=23, minutes=30), "PT20M"),
                  "110000000002": ("Levis 501 Jeans", "LV-501", 15.0, timedelta(days=5), "P25DT2H")}}


def fake_trading(db, acct, call, inner):
    ns = 'xmlns="urn:ebay:apis:eBLBaseComponents"'
    if call == "GetMyeBaySelling":
        items = []
        for iid in STATE["active"]:
            title, sku, price, age, left = STATE["meta"][iid]
            start = NOW - age
            items.append(f"""<Item><ItemID>{iid}</ItemID><Title>{title}</Title>
              <SKU>{sku}</SKU><Quantity>1</Quantity><QuantityAvailable>1</QuantityAvailable>
              <ListingType>FixedPriceItem</ListingType><ListingDuration>GTC</ListingDuration>
              <ListingDetails><StartTime>{iso(start)}</StartTime><ViewItemURL>https://www.ebay.com/itm/{iid}</ViewItemURL></ListingDetails>
              <SellingStatus><CurrentPrice currencyID="USD">{price:.2f}</CurrentPrice></SellingStatus>
              <TimeLeft>{left}</TimeLeft><WatchCount>2</WatchCount></Item>""")
        xml = f"""<GetMyeBaySellingResponse {ns}><Ack>Success</Ack><ActiveList><ItemArray>{''.join(items)}</ItemArray>
          <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult></ActiveList></GetMyeBaySellingResponse>"""
        return ET.fromstring(xml)
    if call == "EndFixedPriceItem":
        iid = re.search(r"<ItemID>(\d+)</ItemID>", inner).group(1)
        STATE["active"].remove(iid)
        return ET.fromstring(f"<EndFixedPriceItemResponse {ns}><Ack>Success</Ack></EndFixedPriceItemResponse>")
    if call == "RelistFixedPriceItem":
        new = "110000000099"
        price = re.search(r"<StartPrice>([\d.]+)</StartPrice>", inner)
        STATE["active"].append(new)
        STATE["meta"][new] = ("Vintage Pyrex Bowl", "PYX-1", float(price.group(1)) if price else 40.0, timedelta(0), "P30D")
        return ET.fromstring(f"<RelistFixedPriceItemResponse {ns}><Ack>Success</Ack><ItemID>{new}</ItemID></RelistFixedPriceItemResponse>")
    raise AssertionError(call)


ORDERS = [
    {"orderId": "01-111", "creationDate": iso(NOW - timedelta(days=3)), "orderPaymentStatus": "PAID",
     "orderFulfillmentStatus": "FULFILLED", "cancelStatus": {"cancelState": "NONE_REQUESTED"},
     "buyer": {"username": "buyer_a"}, "totalMarketplaceFee": {"value": "4.10"},
     "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {"contactAddress": {"city": "Roanoke", "stateOrProvince": "VA", "postalCode": "24011", "countryCode": "US"}}}}],
     "paymentSummary": {"refunds": []},
     "lineItems": [{"lineItemId": "L1", "legacyItemId": "110000000002", "sku": "LV-501", "title": "Levis 501 Jeans", "quantity": 1,
                    "lineItemCost": {"value": "30.00"}, "deliveryCost": {"shippingCost": {"value": "5.00"}},
                    "ebayCollectAndRemitTaxes": [{"amount": {"value": "2.10"}}]}]},
    {"orderId": "01-222", "creationDate": iso(NOW - timedelta(days=200)), "orderPaymentStatus": "FULLY_REFUNDED",
     "orderFulfillmentStatus": "FULFILLED", "cancelStatus": {"cancelState": "NONE_REQUESTED"},
     "totalMarketplaceFee": {"value": "2.00"}, "paymentSummary": {"refunds": [{"amount": {"value": "20.00"}, "refundDate": iso(NOW - timedelta(days=190))}]},
     "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {"contactAddress": {"city": "Toronto", "stateOrProvince": "ON", "countryCode": "CA"}}}}],
     "lineItems": [{"lineItemId": "L2", "legacyItemId": "110000000050", "title": "Old Camera", "quantity": 1,
                    "lineItemCost": {"value": "20.00"}, "deliveryCost": {"shippingCost": {"value": "0"}}}]},
    {"orderId": "01-333", "creationDate": iso(NOW - timedelta(days=100)), "orderPaymentStatus": "PAID",
     "cancelStatus": {"cancelState": "CANCELED"}, "totalMarketplaceFee": {"value": "1.00"},
     "lineItems": [{"lineItemId": "L3", "legacyItemId": "110000000060", "title": "Cancelled Thing", "quantity": 1,
                    "lineItemCost": {"value": "12.00"}}]},
]

TXNS = [
    {"transactionId": "T1", "transactionType": "SALE", "bookingEntry": "CREDIT", "orderId": "01-111", "amount": {"value": "30.90", "currency": "USD"},
     "totalFeeAmount": {"value": "4.10"}, "transactionDate": iso(NOW - timedelta(days=3)),
     "orderLineItems": [{"lineItemId": "L1", "marketplaceFees": [{"feeType": "FINAL_VALUE_FEE", "amount": {"value": "3.80"}}, {"feeType": "FINAL_VALUE_FEE_FIXED_PER_ORDER", "amount": {"value": "0.30"}}]}]},
    {"transactionId": "T2", "transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT", "orderId": "01-111", "amount": {"value": "4.50"}, "transactionDate": iso(NOW - timedelta(days=2))},
    {"transactionId": "T3", "transactionType": "SHIPPING_LABEL", "bookingEntry": "DEBIT", "orderId": "01-111", "amount": {"value": "4.50"}, "transactionDate": iso(NOW - timedelta(days=2))},
    {"transactionId": "T4", "transactionType": "NON_SALE_CHARGE", "bookingEntry": "DEBIT", "orderId": "01-111", "feeType": "AD_FEE", "amount": {"value": "1.20"}, "transactionDate": iso(NOW - timedelta(days=3))},
    {"transactionId": "T5", "transactionType": "NON_SALE_CHARGE", "bookingEntry": "DEBIT", "feeType": "SUBSCRIPTION_FEE", "amount": {"value": "21.95"}, "transactionDate": iso(NOW - timedelta(days=10))},
    {"transactionId": "T6", "transactionType": "CREDIT", "bookingEntry": "CREDIT", "orderId": "01-333", "feeType": "FINAL_VALUE_FEE", "amount": {"value": "1.00"}, "transactionDate": iso(NOW - timedelta(days=99))},
]


def fake_rest(db, acct, method, path, host="api", params=None, json=None, ok=(200, 201, 204)):
    if path == "/sell/fulfillment/v1/order":
        f = params["filter"]
        lo, hi = re.search(r"\[(.*?)\.\.(.*?)\]", f).groups()
        lo = datetime.fromisoformat(lo.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(hi.replace("Z", "+00:00")) if hi else NOW + timedelta(days=1)
        out = [o for o in ORDERS if lo <= datetime.fromisoformat(o["creationDate"].replace("Z", "+00:00")) <= hi] \
            if f.startswith("creationdate") else ORDERS
        return {"orders": out if params.get("offset", 0) == 0 else [], "total": len(out)}
    if path == "/sell/finances/v1/transaction":
        lo, hi = re.search(r"\[(.*?)\.\.(.*?)\]", params["filter"]).groups()
        lo = datetime.fromisoformat(lo.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(hi.replace("Z", "+00:00"))
        out = [t for t in TXNS if lo <= datetime.fromisoformat(t["transactionDate"].replace("Z", "+00:00")) <= hi]
        return {"transactions": out if params.get("offset", 0) == 0 else [], "total": len(out)}
    if path.startswith("/sell/marketing/v1/ad_campaign/") and method == "POST":
        return {}
    if path == "/commerce/identity/v1/user/":
        return {"username": "test_seller", "userId": "U1"}
    raise AssertionError(path)


ebay.trading = fake_trading
ebay.rest = fake_rest


# ----------------------------------------------------------------- helpers
def csrf(c, url):
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:500])
    m = re.search(r'name="_csrf" value="([^"]+)"', r.text)
    return m.group(1) if m else None


def post(c, url, data, from_url=None, files=None):
    tok = csrf(c, from_url or url)
    r = c.post(url, data={**data, "_csrf": tok}, files=files, follow_redirects=False)
    assert r.status_code in (200, 303), (url, r.status_code, r.text[:800])
    return r


def get_ok(c, url):
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:1500])
    return r


def main():
    with TestClient(app) as c:
        # first run → setup
        r = c.get("/", follow_redirects=False)
        assert r.headers["location"] == "/setup"
        post(c, "/setup", {"username": "admin", "password": "password123", "password2": "password123"})
        get_ok(c, "/settings/ebay")
        # eBay keys + public URL
        post(c, "/settings/ebay/keys", {"EBAY_ENVIRONMENT": "production", "EBAY_CLIENT_ID": "App-PRD-1",
                                        "EBAY_CLIENT_SECRET": "PRD-secret", "EBAY_RUNAME": "Me-App-PRD-abc",
                                        "EBAY_MARKETPLACE_ID": "EBAY_US", "PUBLIC_URL": "https://flip.example.com/"},
             "/settings/ebay")
        page = get_ok(c, "/settings/ebay").text
        assert "https://flip.example.com/ebay/account-deletion" in page
        # account deletion challenge
        from app import settings
        tok = settings.get("EBAY_VERIFICATION_TOKEN")
        r = c.get("/ebay/account-deletion?challenge_code=abc123")
        import hashlib
        assert r.json()["challengeResponse"] == hashlib.sha256(
            ("abc123" + tok + "https://flip.example.com/ebay/account-deletion").encode()).hexdigest()
        r = c.post("/ebay/account-deletion", content=json.dumps({"notification": {"data": {"username": "x"}}}))
        assert r.status_code == 204
        # consent URL
        r = c.get("/ebay/connect", follow_redirects=False)
        assert "auth.ebay.com/oauth2/authorize" in r.headers["location"] and "Me-App-PRD-abc" in r.headers["location"]

        # simulate a connected account
        from app.db import SessionLocal
        from app.models import EbayAccount
        from app.util import encrypt, utcnow
        db = SessionLocal()
        db.add(EbayAccount(username="test_seller", access_token=encrypt("tok"), access_expires=utcnow() + timedelta(hours=1),
                           refresh_token=encrypt("rtok"), refresh_expires=utcnow() + timedelta(days=500)))
        db.commit()
        db.close()

        from app.services import scheduler
        msg = scheduler.run_job("full_sync")
        print("SYNC:", msg)
        assert "ERROR" not in msg, msg

        # verify computed sale
        from app.models import Expense, Item, Listing, Sale
        db = SessionLocal()
        s = db.query(Sale).filter_by(order_id="01-111").one()
        assert s.sale_price == 30 and s.shipping_charged == 5 and s.tax_collected == 2.1
        assert abs(s.marketplace_fees - 4.10) < 0.001, s.marketplace_fees
        assert abs(s.shipping_cost - 9.00) < 0.001 and abs(s.ad_fees - 1.2) < 0.001
        assert s.item and s.item.sku == "LV-501" and s.item.status == "sold"
        assert db.query(Sale).filter_by(order_id="01-222").one().status == "refunded"
        canc = db.query(Sale).filter_by(order_id="01-333").one()
        assert canc.status == "cancelled" and canc.restocked
        assert db.query(Expense).filter_by(source="ebay_sync").count() == 1
        jeans_id = s.item_id
        pyrex = db.query(Listing).filter_by(ebay_item_id="110000000001").one()
        assert pyrex.item.status == "listed"
        pyrex_id = pyrex.item_id
        db.close()

        # add cost via quick edit
        post(c, "/inventory/quick-edit", {"id": [str(jeans_id), str(pyrex_id)], f"cost_{jeans_id}": "6.00",
                                          f"source_{jeans_id}": "Goodwill", f"purchase_date_{jeans_id}": "2026-01-10",
                                          f"cost_{pyrex_id}": "3.00", f"storage_location_{pyrex_id}": "Shelf A2"},
             "/inventory/quick-edit")
        db = SessionLocal()
        s = db.query(Sale).filter_by(order_id="01-111").one()
        assert s.cogs == 6.0 and abs(s.net_profit - (35 - 4.1 - 1.2 - 9 - 6)) < 0.01, s.net_profit
        db.close()

        # automations — dry run then live with reprice
        post(c, "/settings/automation", {"AUTOMATION_DRY_RUN": "on", "RELIST_ENABLED": "on", "RELIST_WINDOW_HOURS": "1",
                                         "RELIST_MIN_AGE_DAYS": "30", "RELIST_SKIP_WATCHERS": "0", "RELIST_MAX_PER_RUN": "50",
                                         "REPRICE_ENABLED": "on", "REPRICE_PERCENT": "10", "REPRICE_MAX_REDUCTION_PERCENT": "30",
                                         "REPRICE_AFTER_DAYS": "0", "PROMOTE_ENABLED": "on", "PROMOTE_AFTER_DAYS": "0",
                                         "PROMOTE_CAMPAIGN_ID": "C1", "PROMOTE_AD_RATE": "3"}, "/automations")
        print("RELIST DRY:", scheduler.run_job("relist"))
        post(c, "/settings/automation", {"RELIST_ENABLED": "on", "RELIST_WINDOW_HOURS": "1", "RELIST_MIN_AGE_DAYS": "30",
                                         "RELIST_SKIP_WATCHERS": "0", "RELIST_MAX_PER_RUN": "50", "REPRICE_ENABLED": "on",
                                         "REPRICE_PERCENT": "10", "REPRICE_MAX_REDUCTION_PERCENT": "30", "REPRICE_AFTER_DAYS": "0",
                                         "PROMOTE_ENABLED": "on", "PROMOTE_AFTER_DAYS": "0", "PROMOTE_CAMPAIGN_ID": "C1",
                                         "PROMOTE_AD_RATE": "3"}, "/automations")
        res = scheduler.run_job("relist")
        print("RELIST LIVE:", res)
        assert res.startswith("1 relisted"), res
        db = SessionLocal()
        new = db.query(Listing).filter_by(ebay_item_id="110000000099").one()
        assert new.item_id == pyrex_id and abs(new.price - 36.0) < 0.001, new.price
        db.close()
        res = scheduler.run_job("promote")
        print("PROMOTE:", res)
        assert res.startswith("2 promoted"), res
        print("LISTINGS AGAIN:", scheduler.run_job("sync_listings"))
        ORDERS.append({"orderId": "01-444", "creationDate": iso(NOW - timedelta(minutes=5)), "orderPaymentStatus": "PAID",
                       "cancelStatus": {"cancelState": "NONE_REQUESTED"}, "totalMarketplaceFee": {"value": "4.00"},
                       "lineItems": [{"lineItemId": "L4", "legacyItemId": "110000000099", "sku": "PYX-1", "title": "Vintage Pyrex Bowl",
                                      "quantity": 1, "lineItemCost": {"value": "36.00"}}]})
        res = scheduler.run_job("sync_orders")
        print("ORDERS INCREMENTAL:", res)
        assert "1 new sales" in res, res
        from app.models import Notification
        db = SessionLocal()
        n = db.query(Notification).filter_by(kind="sale").one()
        assert "Shelf A2" in n.body, n.body
        assert db.get(Item, pyrex_id).status == "sold"
        db.close()

        # manual inventory / bulk buys / sales / ledger / mileage
        post(c, "/inventory/new", {"title": "Corningware set", "quantity": "1", "cost": "5", "source": "Estate sale",
                                   "purchase_date": "2026-09-01", "storage_location": "Bin 4",
                                   "relist_enabled": "on"}, "/inventory/new",
             files={"photo": ("p.png", b"\x89PNG\r\n\x1a\n", "image/png")})
        post(c, "/bulk-buys/new", {"name": "Storage unit", "purchase_date": "2026-08-01", "total_cost": "100",
                                   "source": "Auction", "titles": "Lamp\nRadio\nTypewriter"}, "/bulk-buys")
        db = SessionLocal()
        lamp = db.query(Item).filter_by(title="Lamp").one()
        assert abs(lamp.cost - 33.33) < 0.01
        lamp_id = lamp.id
        db.close()
        post(c, "/sales/new", {"marketplace": "Poshmark", "sale_date": "2026-09-20", "title": "Lamp", "quantity": "1",
                               "sale_price": "60", "shipping_charged": "0", "marketplace_fees": "12", "status": "completed",
                               "item_id": f"#{lamp_id} Lamp"}, "/sales/new")
        post(c, "/ledger/save", {"expense_date": "2026-09-02", "category": "Supplies", "amount": "18.99",
                                 "description": "Mailers"}, "/ledger")
        post(c, "/ledger/save", {"expense_date": "2026-09-02", "category": "Meals (50% deductible)", "amount": "20"}, "/ledger")
        post(c, "/mileage/save", {"trip_date": "2026-09-14", "miles": "10", "round_trip": "on", "purpose": "Sourcing"}, "/mileage")

        for url in ["/", "/?period=all", "/?period=custom&start=2025-01-01&end=2026-12-31", "/inventory", "/inventory?status=sold",
                    "/inventory?status=all&missing=cost", f"/inventory/{pyrex_id}", f"/inventory/{pyrex_id}/edit",
                    "/inventory/new", "/listings", "/listings?show=all", "/bulk-buys", "/bulk-buys/1", "/sales",
                    "/sales?period=all", "/sales?q=01-111", "/sales/1", "/sales/new", "/ledger", "/mileage",
                    "/reports/income", "/reports/income?period=all", "/reports/tax", "/reports/tax?method=cash",
                    "/automations", "/issues", "/notifications", "/jobs", "/settings", "/settings?tab=alerts",
                    "/settings?tab=tax", "/settings?tab=system", "/settings?tab=users", "/settings?tab=about", "/data",
                    "/privacy", "/inventory/quick-edit"]:
            get_ok(c, url)
        issues = get_ok(c, "/issues").text
        assert "Duplicate shipping label" in issues
        tax = get_ok(c, "/reports/tax?year=" + str(NOW.year)).text
        assert "Gross receipts" in tax
        r = c.get(f"/reports/tax?year={NOW.year}&format=csv")
        assert r.status_code == 200 and "Net profit" in r.text
        print(r.text)
        for k in ["inventory", "sales", "expenses", "mileage"]:
            assert c.get(f"/data/export/{k}.csv").status_code == 200
        # CSV import
        csvdata = b"title,sku,cost,purchase price,quantity,source,date purchased,location\nTest Mug,MUG1,,2.50,1,Thrift,2026-09-10,Shelf B\n"
        post(c, "/data/import/inventory", {}, "/data", files={"file": ("i.csv", csvdata, "text/csv")})
        # backups + full export/restore round trip
        post(c, "/data/backup-now", {}, "/data")
        exp = c.get("/data/export-all.json.gz")
        assert exp.status_code == 200
        post(c, "/data/restore", {"confirm": "REPLACE"}, "/data", files={"file": ("e.json.gz", exp.content, "application/gzip")})
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303  # signed out after restore
        tok = csrf(c, "/login")
        c.post("/login", data={"username": "admin", "password": "password123", "_csrf": tok})
        get_ok(c, "/")
        db = SessionLocal()
        assert db.query(Sale).count() >= 4
        db.close()
        print("ALL GOOD")


if __name__ == "__main__":
    main()
