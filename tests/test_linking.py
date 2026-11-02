"""Linking eBay listings ⇄ inventory items by type-to-search.

Run:  python tests/test_linking.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import timedelta

if "DATA_DIR" not in os.environ:
    _t = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _t + "/data"
    os.environ["CONFIG_DIR"] = _t + "/config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main():
    from app.db import SessionLocal
    from app.models import Item, ItemEvent, Listing, Sale
    from app.util import utcnow

    with TestClient(app) as c:
        assert c.get("/api/search/listings?q=x", follow_redirects=False).status_code in (303, 401)  # needs sign-in
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/setup").text).group(1)
        c.post("/setup", data={"username": "admin", "password": "password123", "password2": "password123", "_csrf": tok})

        with SessionLocal() as db:
            now = utcnow()

            def listing(eid, title, sku=None, status="active", prev=None, item=None, price=20.0):
                l = Listing(ebay_item_id=eid, title=title, sku=sku, status=status, price=price, quantity=1, quantity_sold=0,
                            start_time=now - timedelta(days=int(eid[-2:])), previous_ebay_item_id=prev,
                            item_id=item.id if item else None)
                db.add(l)
                return l

            # what sync does for an unknown listing: auto-create a placeholder item
            ph = Item(title="Vintage Pyrex Butterprint Bowl 403 Turquoise", status="listed", current_ebay_item_id="110000000011")
            db.add(ph); db.flush()
            listing("110000000010", "Vintage Pyrex Butterprint Bowl 403 Turquoise", status="relisted", item=ph)
            listing("110000000011", "Vintage Pyrex Butterprint Bowl 403 Turquoise", sku="PYX-403", prev="110000000010", item=ph)
            db.add(ItemEvent(item_id=ph.id, kind="created", detail="Imported from eBay listing"))
            db.add(Sale(marketplace="eBay", order_id="O-1", line_item_id="1", item_id=ph.id, ebay_item_id="110000000010",
                        sale_date=now, sale_price=30, status="cancelled", restocked=True))
            # the real item the user logged when they bought it
            real = Item(title="Pyrex butterprint bowl (turquoise) 2.5qt", cost=4.0, source="Goodwill", status="unlisted",
                        storage_location="Shelf A")
            # other inventory + listings
            cam = Item(title="Canon AE-1 Program camera", cost=25.0, status="unlisted")
            lev = Item(title="Levi's 501 jeans 32x30", cost=6.0, status="unlisted")
            sold = Item(title="Pyrex sold already", cost=2.0, status="sold")
            db.add_all([real, cam, lev, sold]); db.flush()
            listing("110000000020", "Canon AE-1 Program 35mm Film Camera Body")
            listing("110000000021", "Levis 501 Jeans 32 x 30 Dark Wash")
            listing("110000000022", "Old ended listing camera", status="ended")
            other_real = Item(title="Fiesta plate", cost=3.0, status="listed")
            db.add(other_real); db.flush()
            listing("110000000023", "Fiestaware Cobalt Dinner Plate", item=other_real)
            other_real.current_ebay_item_id = "110000000023"
            db.commit()
            ids = {k: v.id for k, v in dict(ph=ph, real=real, cam=cam, lev=lev, sold=sold, other=other_real).items()}

        # ---- search listings (inventory → eBay)
        r = c.get(f"/api/search/listings?q=bowl pyrex&item={ids['real']}").json()["results"]
        assert r and r[0]["value"] == "110000000011", r     # word order doesn't matter; relisted copy excluded (not active)
        assert c.get("/api/search/listings?q=PYX-403").json()["results"][0]["value"] == "110000000011"   # by SKU
        assert c.get("/api/search/listings?q=000020").json()["results"][0]["value"] == "110000000020"   # by item #
        assert not c.get("/api/search/listings?q=ended").json()["results"]                             # active only
        assert c.get("/api/search/listings?q=ended&ended=1").json()["results"]
        sug = c.get(f"/api/search/listings?item={ids['real']}").json()
        assert sug["suggested"] and sug["results"][0]["value"] == "110000000011", sug
        sug = c.get(f"/api/search/listings?item={ids['cam']}").json()["results"]
        assert sug[0]["value"] == "110000000020" and all(x["value"] != "110000000023" for x in sug)  # linked-to-real excluded
        print("✓ listing search: any word order, SKU, item #, active-only; suggestions by title similarity")

        # ---- search items (eBay → inventory)
        r = c.get("/api/search/items?q=levi 501&listing=110000000021").json()["results"]
        assert r[0]["value"] == str(ids["lev"])
        r = c.get("/api/search/items?q=pyrex&listing=110000000011").json()["results"]
        vals = [x["value"] for x in r]
        assert str(ids["real"]) in vals and str(ids["ph"]) not in vals and str(ids["sold"]) not in vals, r  # placeholder & sold hidden
        assert c.get(f"/api/search/items?q=%23{ids['cam']}").json()["results"][0]["value"] == str(ids["cam"])  # by #id
        assert c.get("/api/search/items?q=shelf a").json()["results"][0]["value"] == str(ids["real"])        # by location
        sug = c.get("/api/search/items?listing=110000000020").json()
        assert sug["suggested"] and sug["results"][0]["value"] == str(ids["cam"])
        print("✓ item search: title words, #id, location; hides sold and auto-created placeholders; suggestions")

        # ---- link from inventory side: real item ⇄ placeholder-owned listing → merge
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get(f"/inventory/{ids['real']}").text).group(1)
        r = c.post(f"/inventory/{ids['real']}/link", data={"_csrf": tok, "ebay_item_id": "110000000011"})
        assert "merged in" in r.text, r.text[:300]
        with SessionLocal() as db:
            real = db.get(Item, ids["real"])
            assert db.get(Item, ids["ph"]) is None                                  # placeholder gone
            assert sorted(l.ebay_item_id for l in real.listings) == ["110000000010", "110000000011"]  # whole relist chain
            assert real.current_ebay_item_id == "110000000011" and real.status == "listed" and real.sku == "PYX-403"
            assert real.cost == 4.0 and real.storage_location == "Shelf A"          # user's data kept
            assert db.query(Sale).filter_by(order_id="O-1").one().item_id == real.id
            assert any("Imported from eBay listing" in (e.detail or "") for e in db.query(ItemEvent).filter_by(item_id=real.id))
        print("✓ inventory → eBay: placeholder merged (relist chain, sales, timeline moved; cost/location kept)")

        # ---- link from eBay side
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/listings").text).group(1)
        r = c.post("/listings/110000000020/link?next=/listings%3Fshow%3Dall", data={"_csrf": tok, "item_id": str(ids["cam"])},
                   follow_redirects=False)
        assert r.headers["location"] == "/listings?show=all"
        with SessionLocal() as db:
            cam = db.get(Item, ids["cam"])
            assert [l.ebay_item_id for l in cam.listings] == ["110000000020"] and cam.status == "listed"
        print("✓ eBay → inventory: unlinked listing attached, item marked listed, returns to the page you were on")

        # ---- moving a listing away from a REAL item doesn't delete that item
        r = c.post("/listings/110000000023/link", data={"_csrf": tok, "item_id": str(ids["lev"])})
        with SessionLocal() as db:
            other = db.get(Item, ids["other"])
            assert other is not None and not other.listings and other.status == "unlisted"
            assert db.get(Item, ids["lev"]).current_ebay_item_id == "110000000023"
        print("✓ re-pointing a listing from a real item moves only the listing (old item kept, back to unlisted)")

        # ---- bad input
        r = c.post(f"/inventory/{ids['lev']}/link", data={"_csrf": tok, "ebay_item_id": "999"})
        assert "Pick a listing" in r.text
        r = c.post("/listings/110000000021/link", data={"_csrf": tok, "item_id": ""})
        assert "Pick an inventory item" in r.text

        # ---- item page "Merge" keeps the merged item's listings and sales (regression)
        with SessionLocal() as db:
            a = Item(title="Keep me", cost=1.0, status="unlisted")
            b = Item(title="Merge me", status="listed")
            db.add_all([a, b]); db.flush()
            db.add(Listing(ebay_item_id="110000000099", title="Merge me", status="active", item_id=b.id, quantity=1))
            db.add(Sale(marketplace="eBay", order_id="O-9", line_item_id="1", item_id=b.id, sale_date=utcnow(), sale_price=5))
            db.commit()
            aid, bid = a.id, b.id
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get(f"/inventory/{aid}").text).group(1)
        c.post(f"/inventory/{aid}/merge", data={"_csrf": tok, "other_id": str(bid)})
        with SessionLocal() as db:
            assert db.get(Item, bid) is None
            assert db.query(Listing).filter_by(ebay_item_id="110000000099").one().item_id == aid
            assert db.query(Sale).filter_by(order_id="O-9").one().item_id == aid
        print("✓ item-page Merge keeps listings and sales (previously they were orphaned)")

        for u in ["/inventory", f"/inventory/{ids['lev']}", "/listings", "/listings?show=all"]:
            assert c.get(u).status_code == 200, u
    with TestClient(app) as anon:
        r = anon.get("/api/search/items?q=pyrex", follow_redirects=False)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"
        print("✓ search API requires sign-in (401 JSON)")
    print("LINKING ALL GOOD")


if __name__ == "__main__":
    main()
