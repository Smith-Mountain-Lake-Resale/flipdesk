from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import settings
from ..db import get_db
from ..models import EbayAccount
from ..services import reports
from ..util import utcnow
from ..web import render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    period = p.get("period") or "90d"
    start, end, label = reports.period_range(period, p.get("start"), p.get("end"), db)
    data = reports.insights(db, start, end)
    inv = reports.inventory_snapshot(db)
    acct = db.query(EbayAccount).first()
    return render(request, "dashboard.html", {
        "d": data, "inv": inv, "period": period, "label": label, "p": p, "start": start, "end": end,
        "acct": acct, "series_json": json.dumps(data["series"]),
        "mkt_json": json.dumps({"labels": [k for k, _ in data["by_marketplace"]],
                                "net": [round(v["net"], 2) for _, v in data["by_marketplace"]]}),
        "src_json": json.dumps({"labels": [k for k, _ in data["top_sources"]],
                                "net": [round(v["net"], 2) for _, v in data["top_sources"]]}),
        "lbs_json": json.dumps({"labels": [k for k, _ in data["listings_by_source"]],
                                "count": [v for _, v in data["listings_by_source"]]}),
        "checks": [c for c in reports.data_checks(db, utcnow().year) if c["count"] and c["severity"] == "high"],
    })


@router.get("/reports/income")
def income(request: Request, db: Session = Depends(get_db)):
    p = dict(request.query_params)
    period = p.get("period") or "ytd"
    start, end, label = reports.period_range(period, p.get("start"), p.get("end"), db)
    return render(request, "income.html", {"r": reports.income_statement(db, start, end), "p": p,
                                           "period": period, "label": label, "start": start, "end": end})


@router.get("/reports/tax")
def tax(request: Request, db: Session = Depends(get_db)):
    year = int(request.query_params.get("year") or utcnow().year)
    method = request.query_params.get("method") or settings.get("TAX_METHOD")
    r = reports.tax_report(db, year, method)
    if request.query_params.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Schedule C", "Description", "Amount"])
        w.writerow(["1", "Gross receipts or sales", r["line1"]])
        w.writerow(["2", "Returns and allowances", r["line2"]])
        w.writerow(["3", "Subtract line 2 from line 1", r["line3"]])
        w.writerow(["35", "Inventory at beginning of year", r["part3"]["begin"]])
        w.writerow(["36", "Purchases", r["part3"]["purchases"]])
        w.writerow(["41", "Inventory at end of year", r["part3"]["end"]])
        w.writerow(["4 / 42", "Cost of goods sold", r["line4"]])
        w.writerow(["5", "Gross profit", r["line5"]])
        for e in r["expense_lines"]:
            w.writerow([e["line"], e["label"], e["amount"]])
        w.writerow(["28", "Total expenses", r["line28"]])
        w.writerow(["31", "Net profit (loss)", r["line31"]])
        w.writerow([])
        w.writerow(["Info", "Sales tax collected & remitted by marketplaces (not income)", r["tax_collected"]])
        for k, v in r["gross_by_marketplace"].items():
            w.writerow(["Info", f"Gross receipts — {k}", v])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="flipdesk-tax-{year}-{method}.csv"'})
    years = list(range(utcnow().year, utcnow().year - 6, -1))
    return render(request, "tax.html", {"r": r, "years": years, "year": year, "method": method})
