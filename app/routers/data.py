from __future__ import annotations

import gzip
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..services import data_io
from ..util import utcnow
from ..web import form, redirect, render

router = APIRouter()

SAMPLES = {
    "inventory": "title,sku,cost,quantity,source,purchase_date,storage_location,notes\n"
                 "Vintage Pyrex bowl,PYX-001,4.99,1,Goodwill,2026-09-14,Shelf A2,\n",
    "sales": "marketplace,order_id,sale_date,title,sku,quantity,sale_price,shipping_charged,marketplace_fees,shipping_cost,cost,source,purchase_date\n"
             "Poshmark,PM-123,2026-09-20,Levi's 501 jeans,LV-501,1,38.00,0,7.60,0,6.00,Thrift store,2026-08-30\n",
    "expenses": "date,category,description,vendor,amount\n2026-09-01,Supplies,Poly mailers 100ct,Amazon,18.99\n",
    "mileage": "date,miles,purpose\n2026-09-14,23.4,Sourcing — estate sale\n",
}


@router.get("/data")
def data_page(request: Request):
    return render(request, "data.html", {"backups": data_io.list_backups(), "is_sqlite": config.IS_SQLITE,
                                         "db": config.redacted_db_url()})


@router.get("/data/export/{kind}.csv")
def export_csv(kind: str, db: Session = Depends(get_db)):
    if kind not in data_io.CSV_EXPORTS:
        raise HTTPException(404)
    stamp = utcnow().strftime("%Y%m%d")
    return Response(data_io.export_csv(db, kind), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="flipdesk-{kind}-{stamp}.csv"'})


@router.get("/data/sample/{kind}.csv")
def sample_csv(kind: str):
    if kind not in SAMPLES:
        raise HTTPException(404)
    return Response(SAMPLES[kind], media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="flipdesk-{kind}-template.csv"'})


@router.get("/data/export-all.json.gz")
def export_all(db: Session = Depends(get_db)):
    body = gzip.compress(json.dumps(data_io.export_all(db)).encode())
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return Response(body, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="flipdesk-export-{stamp}.json.gz"'})


@router.post("/data/import/{kind}")
async def import_csv(kind: str, request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    up = f.get("file")
    if kind not in SAMPLES or not up or not getattr(up, "filename", ""):
        return redirect("/data", request, "Choose a CSV file.", "err")
    n, errors = data_io.import_csv(db, kind, await up.read())
    msg = f"Imported {n} {kind} rows."
    if errors:
        msg += f" {len(errors)} skipped: " + "; ".join(errors[:5])
    return redirect("/data", request, msg, "err" if errors and not n else "ok")


@router.post("/data/restore")
async def restore(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    if f.get("confirm") != "REPLACE":
        return redirect("/data", request, "Type REPLACE to confirm — this overwrites all current data.", "err")
    up = f.get("file")
    if not up or not getattr(up, "filename", ""):
        return redirect("/data", request, "Choose an export file.", "err")
    raw = await up.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        payload = json.loads(raw)
    except ValueError:
        return redirect("/data", request, "That isn't a FlipDesk JSON export.", "err")
    data_io.run_backup(db)  # safety copy first
    counts = data_io.import_all(db, payload)
    request.session.clear()
    return redirect("/login", None, None) if counts else redirect("/data", request, "Nothing imported.", "err")


@router.post("/data/backup-now")
async def backup_now(request: Request, db: Session = Depends(get_db)):
    await form(request)
    return redirect("/data", request, data_io.run_backup(db))


@router.get("/data/backups/{name}")
def download_backup(name: str):
    p = (data_io.BACKUP_DIR / name).resolve()
    if p.parent != data_io.BACKUP_DIR.resolve() or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, filename=name)
