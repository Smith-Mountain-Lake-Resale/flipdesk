"""Backups, full JSON export/import (also used to migrate SQLite ⇄ Postgres/MySQL) and CSV."""
from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Date, DateTime, text
from sqlalchemy.orm import Session

from .. import config, settings
from ..db import Base, engine
from ..models import Expense, Item, MileageEntry, Sale
from ..util import parse_date, parse_iso, to_float, utcnow

BACKUP_DIR = config.DATA_DIR / "backups"


def _ser(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def export_all(db: Session) -> dict:
    out = {"app": config.APP_NAME, "version": config.APP_VERSION, "exported_at": utcnow().isoformat(),
           "note": "Encrypted values (eBay tokens, secrets) only decrypt with the same SECRET_KEY / secret.key.",
           "tables": {}}
    for table in Base.metadata.sorted_tables:
        rows = db.execute(table.select()).mappings().all()
        out["tables"][table.name] = [{k: _ser(v) for k, v in r.items()} for r in rows]
    return out


def import_all(db: Session, data: dict) -> dict:
    tables = data.get("tables") or {}
    counts = {}
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        for table in Base.metadata.sorted_tables:
            rows = tables.get(table.name) or []
            if not rows:
                continue
            cols = {c.name: c for c in table.columns}
            clean = []
            for r in rows:
                row = {}
                for k, v in r.items():
                    if k not in cols:
                        continue
                    t = cols[k].type
                    if v is not None and isinstance(t, DateTime):
                        v = parse_iso(v)
                    elif v is not None and isinstance(t, Date):
                        v = parse_date(v)
                    row[k] = v
                clean.append(row)
            for i in range(0, len(clean), 500):
                conn.execute(table.insert(), clean[i:i + 500])
            counts[table.name] = len(clean)
        if engine.dialect.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                if "id" in table.columns and table.columns["id"].primary_key:
                    conn.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}','id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table.name}), 1))"))
    settings.invalidate()
    return counts


def run_backup(db: Session) -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    if config.IS_SQLITE:
        src_path = config.DATABASE_URL.replace("sqlite:///", "", 1)
        dest = BACKUP_DIR / f"flipdesk-{stamp}.db"
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    else:
        dest = BACKUP_DIR / f"flipdesk-{stamp}.json.gz"
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            json.dump(export_all(db), fh)
    keep = int(settings.get("BACKUP_KEEP") or 14)
    files = sorted(BACKUP_DIR.glob("flipdesk-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
    return f"backup written: {dest.name}"


def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("flipdesk-*"), key=lambda p: p.stat().st_mtime, reverse=True)


# ------------------------------------------------------------------ CSV
CSV_EXPORTS = {
    "inventory": (Item, ["id", "sku", "title", "status", "quantity", "quantity_sold", "cost", "source",
                         "purchase_date", "storage_location", "category", "brand", "condition",
                         "current_ebay_item_id", "original_price", "min_price", "listed_at", "sold_at", "notes"]),
    "sales": (Sale, ["id", "marketplace", "order_id", "line_item_id", "sale_date", "title", "sku", "quantity",
                     "sale_price", "shipping_charged", "tax_collected", "marketplace_fees", "ad_fees",
                     "shipping_cost", "other_fees", "refund_amount", "status", "item_id", "buyer_city",
                     "buyer_state", "buyer_country", "notes"]),
    "expenses": (Expense, ["id", "expense_date", "category", "description", "vendor", "amount", "item_id", "source"]),
    "mileage": (MileageEntry, ["id", "trip_date", "miles", "round_trip", "purpose", "start_location", "end_location"]),
}


def export_csv(db: Session, kind: str) -> str:
    model, cols = CSV_EXPORTS[kind]
    buf = io.StringIO()
    w = csv.writer(buf)
    extra = ["cogs", "net_profit"] if kind == "sales" else []
    w.writerow(cols + extra)
    for row in db.query(model).order_by(model.id).all():
        vals = [_ser(getattr(row, c)) for c in cols]
        if kind == "sales":
            vals += [row.cogs, row.net_profit]
        w.writerow(vals)
    return buf.getvalue()


def _norm(h: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in (h or "").strip().lower()).strip("_")


ALIASES = {
    "title": ["title", "name", "item", "item_name", "description", "product"],
    "sku": ["sku", "custom_label", "custom_label_sku", "item_sku"],
    "cost": ["cost", "purchase_price", "cost_of_goods", "cogs", "item_cost", "price_paid", "buy_price"],
    "quantity": ["quantity", "qty"],
    "source": ["source", "purchase_location", "where_purchased", "store", "bought_from"],
    "purchase_date": ["purchase_date", "date_purchased", "bought_on", "acquired", "date_acquired"],
    "storage_location": ["storage_location", "location", "bin", "shelf", "storage"],
    "notes": ["notes", "note", "comments"],
    "status": ["status"],
    "marketplace": ["marketplace", "platform", "channel", "sold_on"],
    "order_id": ["order_id", "order", "order_number"],
    "sale_date": ["sale_date", "date_sold", "sold_date", "date"],
    "sale_price": ["sale_price", "sold_price", "price", "item_price"],
    "shipping_charged": ["shipping_charged", "shipping_paid_by_buyer", "shipping_income"],
    "marketplace_fees": ["marketplace_fees", "fees", "selling_fees", "platform_fees"],
    "shipping_cost": ["shipping_cost", "postage", "label_cost", "shipping_label"],
    "ad_fees": ["ad_fees", "promoted_fees", "advertising"],
    "other_fees": ["other_fees"],
    "expense_date": ["expense_date", "date"],
    "category": ["category", "type"],
    "vendor": ["vendor", "payee", "merchant"],
    "amount": ["amount", "total", "cost"],
    "description": ["description", "memo", "details", "notes"],
    "trip_date": ["trip_date", "date"],
    "miles": ["miles", "distance", "mileage"],
    "purpose": ["purpose", "reason", "description"],
}


def _pick(row: dict, field: str):
    for a in ALIASES.get(field, [field]):
        if a in row and row[a] not in (None, ""):
            return row[a]
    return None


def import_csv(db: Session, kind: str, content: bytes) -> tuple[int, list[str]]:
    textdata = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(textdata))
    n, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        row = {_norm(k): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
        try:
            if kind == "inventory":
                title = _pick(row, "title")
                if not title:
                    raise ValueError("missing title")
                status = (_pick(row, "status") or "unlisted").lower()
                if status not in ("unlisted", "listed", "sold", "written_off"):
                    status = "unlisted"
                db.add(Item(title=title, sku=_pick(row, "sku"), cost=to_float(_pick(row, "cost"), None),
                            quantity=int(to_float(_pick(row, "quantity"), 1) or 1), source=_pick(row, "source"),
                            purchase_date=parse_date(_pick(row, "purchase_date")),
                            storage_location=_pick(row, "storage_location"), notes=_pick(row, "notes"),
                            status=status))
            elif kind == "sales":
                d = parse_iso(_pick(row, "sale_date")) or (
                    datetime.combine(parse_date(_pick(row, "sale_date")), datetime.min.time())
                    if parse_date(_pick(row, "sale_date")) else None)
                if not d:
                    raise ValueError("missing/invalid sale date")
                mkt = _pick(row, "marketplace") or "Other"
                oid = _pick(row, "order_id") or f"import-{utcnow().timestamp():.0f}-{i}"
                sku = _pick(row, "sku")
                cost = to_float(_pick(row, "cost"), None)
                item = db.query(Item).filter_by(sku=sku).first() if sku else None
                if item is None:
                    item = Item(title=_pick(row, "title") or oid, sku=sku, cost=cost,
                                quantity=int(to_float(_pick(row, "quantity"), 1) or 1),
                                source=_pick(row, "source"), purchase_date=parse_date(_pick(row, "purchase_date")),
                                status="sold")
                    db.add(item)
                    db.flush()
                s = Sale(marketplace=mkt, order_id=oid, line_item_id=str(i), item_id=item.id,
                         title=_pick(row, "title") or item.title, sku=sku,
                         quantity=int(to_float(_pick(row, "quantity"), 1) or 1), sale_date=d,
                         sale_price=to_float(_pick(row, "sale_price")) or 0,
                         shipping_charged=to_float(_pick(row, "shipping_charged")) or 0,
                         marketplace_fees=to_float(_pick(row, "marketplace_fees")) or 0,
                         ad_fees=to_float(_pick(row, "ad_fees")) or 0,
                         shipping_cost=to_float(_pick(row, "shipping_cost")) or 0,
                         other_fees=to_float(_pick(row, "other_fees")) or 0, source="import")
                db.add(s)
                db.flush()
                from .sync import refresh_item_status
                db.refresh(item)
                refresh_item_status(db, item)
            elif kind == "expenses":
                d = parse_date(_pick(row, "expense_date"))
                if not d:
                    raise ValueError("missing/invalid date")
                db.add(Expense(expense_date=d, category=_pick(row, "category") or "Other expenses",
                               description=_pick(row, "description"), vendor=_pick(row, "vendor"),
                               amount=to_float(_pick(row, "amount")) or 0, source="import"))
            elif kind == "mileage":
                d = parse_date(_pick(row, "trip_date"))
                if not d:
                    raise ValueError("missing/invalid date")
                db.add(MileageEntry(trip_date=d, miles=to_float(_pick(row, "miles")) or 0,
                                    purpose=_pick(row, "purpose")))
            n += 1
        except Exception as exc:
            errors.append(f"Row {i}: {exc}")
    db.commit()
    return n, errors
