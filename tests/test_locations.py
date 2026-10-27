"""Locations, QR shelf labels and scanning (server side).

Run:  python tests/test_locations.py
PDF QR decoding is checked when OpenCV (cv2) and poppler's pdftoppm are installed.
The browser/camera flow is covered by an E2E run with Chromium's fake camera.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

if "DATA_DIR" not in os.environ:
    _t = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = _t + "/data"
    os.environ["CONFIG_DIR"] = _t + "/config"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def tok(c, url):
    return re.search(r'name="_csrf" value="([^"]+)"', c.get(url).text).group(1)


def post(c, url, data, src):
    return c.post(url, data={**data, "_csrf": tok(c, src)})


def main():
    from app.db import SessionLocal
    from app.models import Item, Location
    from app.services import labels as L
    from app.services import locations as locsvc

    # scan parsing
    assert locsvc.parse_scan("https://flip.example.com/l/L0007") == "L0007"
    assert locsvc.parse_scan("http://10.0.0.5:8080/l/l0012?x=1") == "L0012"
    assert locsvc.parse_scan("L0003") == "L0003"
    assert locsvc.parse_scan("HTTPS://FLIP.EXAMPLE.COM/L/L0007") == "L0007"  # compact upper-case label payload
    assert L.compact("https://flip.example.com/l/L0007") == "HTTPS://FLIP.EXAMPLE.COM/L/L0007"
    assert L.compact("https://flip.example.com/my_app/l/L0007").islower() is False  # "_" can't be upper-cased into alnum mode
    assert L.compact("https://x.example/my_app/l/L1") == "https://x.example/my_app/l/L1"
    assert locsvc.parse_scan("https://example.com/whatever") is None
    print("✓ scan text parsing")

    with TestClient(app) as c:
        post(c, "/setup", {"username": "admin", "password": "password123", "password2": "password123"}, "/setup")
        # free-text location on an item creates a Location automatically
        post(c, "/inventory/new", {"title": "Pyrex bowl", "quantity": "1", "storage_location": "Garage shelf A1"}, "/inventory/new")
        post(c, "/inventory/new", {"title": "Camera", "quantity": "1"}, "/inventory/new")
        with SessionLocal() as db:
            loc = db.query(Location).one()
            bowl = db.query(Item).filter_by(title="Pyrex bowl").one()
            cam = db.query(Item).filter_by(title="Camera").one()
            assert loc.name == "Garage shelf A1" and loc.code == "L0001" and bowl.location_id == loc.id
        print("✓ typing a location on an item creates a labelled location")

        r = post(c, "/locations/new", {"names": "Tote 1-3\nShelf B01-B02\nGarage shelf A1"}, "/locations")
        with SessionLocal() as db:
            names = sorted(l.name for l in db.query(Location))
        assert names == ["Garage shelf A1", "Shelf B01", "Shelf B02", "Tote 1", "Tote 2", "Tote 3"], names
        print("✓ ranges create one location each; duplicates skipped")

        # labels: every layout renders, PDF has the right page size and count
        for key, lay in ((k, L.layout(k)) for k in L.LAYOUTS):
            r = c.get(f"/locations/labels?all=1&layout={key}&copies=2")
            assert r.status_code == 200 and r.text.count('class="cell') == 12, (key, r.text.count('class="cell'))
            assert f"size: {lay['pw']}in {lay['ph']}in" in r.text
            pdf = c.get(f"/locations/labels.pdf?all=1&layout={key}&copies=2")
            assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
            pages = len(re.findall(rb"/Type /Page\b", pdf.content))
            assert pages == -(-12 // lay["per_page"]), (key, pages)
        r = c.get("/locations/labels?all=1&layout=4x6-4&skip=3")
        assert r.text.count('class="page"') == 3  # 3 skipped + 6 labels = 9 cells → 3 pages
        print("✓ all label layouts render (HTML + PDF), copies and skip work")

        if shutil.which("pdftoppm"):
            try:
                import cv2
            except ImportError:
                cv2 = None
            if cv2:
                d = tempfile.mkdtemp()
                open(f"{d}/l.pdf", "wb").write(c.get("/locations/labels.pdf?all=1&layout=4x6-8").content)
                subprocess.run(["pdftoppm", "-r", "203", "-png", f"{d}/l.pdf", f"{d}/p"], check=True)
                det, found, lay = cv2.QRCodeDetector(), set(), L.layout("4x6-8")
                for png in glob.glob(f"{d}/p-*.png"):
                    img = cv2.imread(png, cv2.IMREAD_GRAYSCALE)
                    for row in range(lay["rows"]):
                        for col in range(lay["cols"]):
                            cell = img[int(row * lay["ch"] * 203):int((row + 1) * lay["ch"] * 203),
                                       int(col * lay["cw"] * 203):int((col + 1) * lay["cw"] * 203)]
                            big = cv2.copyMakeBorder(cv2.resize(cell, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST),
                                                     40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
                            t, _, _ = det.detectAndDecode(big)
                            if t:
                                found.add(t.lower())
                assert {f"http://testserver/l/l000{i}" for i in range(1, 7)} <= found, found
                print("✓ smallest 4×6 layout (8-up) QR codes decode at 203 dpi")

        # scanning
        t = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/scan").text).group(1)
        h = {"X-CSRF-Token": t}
        assert c.post("/scan/resolve", json={"text": "L0001"}).status_code == 400  # CSRF required
        r = c.post("/scan/resolve", json={"text": "http://testserver/l/L0003", "mode": "find"}, headers=h)
        assert r.json()["redirect"] == "/locations/L0003", r.text
        assert c.post("/scan/resolve", json={"text": "L0999"}, headers=h).status_code == 404
        assert c.post("/scan/resolve", json={"text": "https://other.example/x"}, headers=h).status_code == 404
        c.get(f"/scan?mode=item&item={cam.id}")
        r = c.post("/scan/resolve", json={"text": "http://testserver/l/L0003", "mode": "item", "item": cam.id}, headers=h)
        assert r.json()["redirect"] == f"/inventory/{cam.id}"
        with SessionLocal() as db:
            cam_loc = db.get(Item, cam.id).location
            assert cam_loc.code == "L0003"
        print("✓ scan resolves labels; item mode moves the item; bad codes rejected")

        # phone camera app flow (/l/CODE) with a pending item scan
        c.get(f"/scan?mode=item&item={bowl.id}")
        r = c.get("/l/L0002")
        assert str(r.url).endswith(f"/inventory/{bowl.id}")
        r = c.get("/l/L0002")  # no pending scan any more → just opens the location
        assert str(r.url).endswith("/locations/L0002")
        assert str(c.get("/L/L0002").url).endswith("/locations/L0002")  # what the upper-case QR opens
        print("✓ /l/CODE links work from a phone camera")

        # put-away, remove, rename (labels keep working), delete
        r = post(c, "/locations/L0004/assign", {"item": [str(bowl.id), str(cam.id)]}, "/locations/L0004?put=1")
        assert "Moved 2 item" in r.text
        post(c, f"/locations/L0004/remove/{cam.id}", {}, "/locations/L0004")
        post(c, "/locations/L0004/edit", {"name": "Tote One", "description": "Garage"}, "/locations/L0004")
        with SessionLocal() as db:
            assert db.get(Item, bowl.id).storage_location == "Tote One"
            assert db.get(Item, cam.id).location_id is None
        assert c.get("/l/L0004").url.path == "/locations/L0004"
        post(c, "/locations/L0004/delete", {}, "/locations/L0004")
        with SessionLocal() as db:
            b = db.get(Item, bowl.id)
            assert b.location_id is None and b.storage_location is None
        print("✓ put-away, remove, rename (QR unchanged) and delete")

        for u in ["/locations", "/locations/L0001", "/locations/L0001?put=1&q=Py", "/scan", f"/inventory/{bowl.id}"]:
            assert c.get(u).status_code == 200, u
    print("LOCATIONS ALL GOOD")


if __name__ == "__main__":
    main()
