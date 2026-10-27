from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Location
from ..services import recon
from ..util import utcnow
from ..web import render

router = APIRouter()


def _opts(request: Request) -> dict:
    p = request.query_params
    scope = p.get("scope") if p.get("scope") in recon.SCOPES else "listed"
    sort = p.get("sort") if p.get("sort") in recon.SORTS else "location"
    loc_ids = [int(x) for x in p.getlist("loc") if x.isdigit()]
    return {"scope": scope, "sort": sort, "loc": loc_ids,
            "unplaced": p.get("unplaced", "1") != "0" if "submitted" not in p else p.get("unplaced") == "1",
            "paper": p.get("paper") if p.get("paper") in recon.PAPERS else "letter",
            "orientation": "landscape" if p.get("orientation") == "landscape" else "portrait",
            "signature": p.get("signature", "1") != "0" if "submitted" not in p else p.get("signature") == "1"}


@router.get("/inventory/reconcile")
def reconcile_page(request: Request, db: Session = Depends(get_db)):
    o = _opts(request)
    data = recon.rows(db, o["scope"], o["loc"], o["unplaced"], o["sort"])
    return render(request, "reconcile.html", {"o": o, "rows": data, "s": recon.summary(data),
                                              "locations": recon.location_choices(db), "scopes": recon.SCOPES,
                                              "sorts": recon.SORTS, "papers": recon.PAPERS,
                                              "qs": request.url.query})


@router.get("/inventory/reconcile.pdf")
def reconcile_pdf(request: Request, db: Session = Depends(get_db)):
    o = _opts(request)
    data = recon.rows(db, o["scope"], o["loc"], o["unplaced"], o["sort"])
    filt = ""
    if o["loc"]:
        names = [l.name for l in db.query(Location).filter(Location.id.in_(o["loc"])).order_by(Location.name)]
        filt = "Locations: " + ", ".join(names[:8]) + (f" +{len(names) - 8} more" if len(names) > 8 else "")
        if o["unplaced"]:
            filt += " + items with no location"
    body = recon.pdf(data, scope=o["scope"], sort=o["sort"], paper=o["paper"], orientation=o["orientation"],
                     filters_text=filt, generated=utcnow(), counter_line=o["signature"])
    name = f"flipdesk-reconciliation-{utcnow():%Y%m%d}.pdf"
    return Response(body, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})
