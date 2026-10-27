"""Inventory reconciliation sheet (page + PDF).

Run:  python tests/test_recon.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile

if "DATA_DIR" not in os.environ:
    _t = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _t + "/data"
    os.environ["CONFIG_DIR"] = _t + "/config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pypdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def text_of(pdf_bytes: bytes) -> str:
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)


def main():
    from app.db import SessionLocal
    from app.models import Item, Listing, Location
    with TestClient(app) as c:
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/setup").text).group(1)
        c.post("/setup", data={"username": "admin", "password": "password123", "password2": "password123", "_csrf": tok})
        with SessionLocal() as db:
            def add(title, loc, status="listed", qty=1, ebay_qty=None, active=True, sku=None):
                it = Item(title=title, status=status, quantity=qty, storage_location=loc, sku=sku)
                db.add(it)
                db.flush()
                if status == "listed":
                    eid = str(120000000000 + it.id)
                    db.add(Listing(ebay_item_id=eid, item_id=it.id, title=title, price=25.0, quantity=ebay_qty or qty,
                                   quantity_sold=0, status="active" if active else "ended"))
                    it.current_ebay_item_id = eid
                return it
            add("Blue Pyrex Bowl", "Shelf A", sku="PYX-1")
            add("Canon AE-1 Camera", "Shelf B", qty=3, ebay_qty=2)
            add("Lonely Lamp", None)
            add("Unlisted Radio", "Shelf A", status="unlisted")
            add("Ended Listing Vase", "Shelf B", active=False)
            sold = add("Sold Jeans", "Shelf A")
            sold.status = "sold"
            db.commit()
            shelf_a = db.query(Location).filter_by(name="Shelf A").one().id

        r = c.get("/inventory/reconcile")
        assert r.status_code == 200 and "Blue Pyrex Bowl" in r.text and "Unlisted Radio" not in r.text
        assert "eBay 2" in r.text  # quantity mismatch flagged
        print("✓ page lists active eBay listings only, flags qty mismatch")

        pdf = c.get("/inventory/reconcile.pdf")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        t = text_of(pdf.content)
        for s in ["Inventory reconciliation", "Blue Pyrex Bowl", "PYX-1", "Canon AE-1 Camera", "Lonely Lamp",
                  "Shelf A", "Shelf B", "No location set", "Expected", "Counted", "Notes", "Counted by", "Page 1",
                  "Not on the list but found on a shelf"]:
            assert s in t, s
        for s in ["Unlisted Radio", "Ended Listing Vase", "Sold Jeans"]:
            assert s not in t, s
        assert t.index("Shelf A") < t.index("Shelf B") < t.index("No location set")  # grouped, unplaced last
        print("✓ PDF: grouped by location, unplaced last, excludes unlisted/ended/sold")

        t = text_of(c.get("/inventory/reconcile.pdf?scope=stock&submitted=1&unplaced=1&signature=1").content)
        assert "Unlisted Radio" in t and "not listed" in t
        t = text_of(c.get(f"/inventory/reconcile.pdf?scope=stock&loc={shelf_a}&submitted=1&signature=0").content)
        assert "Blue Pyrex Bowl" in t and "Canon" not in t and "Lonely Lamp" not in t and "Counted by" not in t
        t = text_of(c.get("/inventory/reconcile.pdf?sort=title&paper=a4&orientation=landscape&submitted=1&unplaced=1").content)
        reader = pypdf.PdfReader(io.BytesIO(c.get("/inventory/reconcile.pdf?paper=a4&orientation=landscape").content))
        box = reader.pages[0].mediabox
        assert round(float(box.width) / 72, 2) == 11.69 and round(float(box.height) / 72, 2) == 8.27
        assert t.index("Blue Pyrex Bowl") < t.index("Canon AE-1") < t.index("Lonely Lamp")
        print("✓ scope, single-location count sheet, title sort, A4 landscape, signature toggle")

        # big list paginates with repeated headers
        with SessionLocal() as db:
            for i in range(120):
                db.add(Item(title=f"Bulk item {i:03d}", status="unlisted", storage_location="Shelf C"))
            db.commit()
        pdf = c.get("/inventory/reconcile.pdf?scope=stock&submitted=1&unplaced=1")
        reader = pypdf.PdfReader(io.BytesIO(pdf.content))
        assert len(reader.pages) >= 4
        t = text_of(pdf.content)
        assert "Shelf C (continued)" in t and t.count("Bulk item") == 120
        print(f"✓ {len(reader.pages)}-page sheet with every item and repeated '(continued)' shelf headers")
    print("RECON ALL GOOD")


if __name__ == "__main__":
    main()
