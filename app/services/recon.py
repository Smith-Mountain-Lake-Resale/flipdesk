"""Inventory reconciliation (shelf count) sheet — printable PDF.

One row per item that should be on a shelf: tick box, item (title, SKU, eBay item #),
location, expected quantity, a box for the counted quantity and space for notes.
Grouped by location (so you walk one shelf at a time) or sorted by title.
"""
from __future__ import annotations

import io
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (CondPageBreak, Flowable, KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..models import Item, Listing, Location

SCOPES = {
    "listed": "Listed on eBay (active listings)",
    "stock": "Everything in stock (listed + unlisted)",
    "unlisted": "Unlisted only (not on eBay yet)",
}
SORTS = {"location": "Grouped by location (walk shelf by shelf)", "title": "Alphabetical by item"}
PAPERS = {"letter": ("Letter", LETTER), "a4": ("A4", A4)}
NO_LOC = "No location set"


def rows(db: Session, scope: str = "listed", location_ids: list[int] | None = None, include_unplaced: bool = True,
         sort: str = "location") -> list[dict]:
    q = db.query(Item).options(selectinload(Item.listings), selectinload(Item.location))
    if scope == "unlisted":
        q = q.filter(Item.status == "unlisted")
    elif scope == "stock":
        q = q.filter(Item.status.in_(("listed", "unlisted")))
    else:  # listed on eBay
        q = q.filter(Item.status == "listed", Item.listings.any(or_(Listing.status == "active", Listing.pending_relist.is_(True))))
    if location_ids:
        cond = Item.location_id.in_(location_ids)
        q = q.filter(or_(cond, Item.location_id.is_(None)) if include_unplaced else cond)
    elif not include_unplaced:
        q = q.filter(Item.location_id.isnot(None))
    out = []
    for it in q.all():
        active = [l for l in it.listings if l.status == "active" or l.pending_relist]
        remaining = max((it.quantity or 1) - (it.quantity_sold or 0), 0)
        ebay_qty = sum(max((l.quantity or 1) - (l.quantity_sold or 0), 0) for l in active) if active else None
        out.append({
            "id": it.id, "title": it.title, "sku": it.sku, "ebay_id": it.current_ebay_item_id if active else None,
            "location": it.location.name if it.location else (it.storage_location or None),
            "code": it.location.code if it.location else None,
            "qty": remaining if remaining else (ebay_qty or 1), "ebay_qty": ebay_qty,
            "price": active[0].price if active else None, "status": it.status,
        })
    if sort == "title":
        out.sort(key=lambda r: (r["title"] or "").lower())
    else:  # by location; unplaced last so they stand out at the end
        out.sort(key=lambda r: (r["location"] is None, (r["location"] or "").lower(), (r["title"] or "").lower()))
    return out


def summary(data: list[dict]) -> dict:
    locs = {r["location"] for r in data if r["location"]}
    return {"items": len(data), "units": sum(r["qty"] for r in data), "locations": len(locs),
            "unplaced": sum(1 for r in data if not r["location"])}


# ------------------------------------------------------------------ PDF
INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#555555")
RULE = colors.HexColor("#9a9a9a")
BAND = colors.HexColor("#e9ecea")


def _styles():
    base = dict(fontName="Helvetica", fontSize=9, leading=11, textColor=INK)
    return {
        "title": ParagraphStyle("t", **{**base, "fontName": "Helvetica-Bold", "fontSize": 16, "leading": 19}),
        "meta": ParagraphStyle("m", **{**base, "fontSize": 8.5, "leading": 11, "textColor": MUTED}),
        "item": ParagraphStyle("i", **{**base, "fontName": "Helvetica-Bold", "fontSize": 9.5, "leading": 11.5}),
        "sub": ParagraphStyle("s", **{**base, "fontSize": 7.5, "leading": 9.5, "textColor": MUTED}),
        "cell": ParagraphStyle("c", **base),
        "head": ParagraphStyle("h", **{**base, "fontName": "Helvetica-Bold", "fontSize": 8, "textColor": INK}),
        "qty": ParagraphStyle("q", **{**base, "fontName": "Helvetica-Bold", "fontSize": 11, "alignment": TA_CENTER}),
        "group": ParagraphStyle("g", **{**base, "fontName": "Helvetica-Bold", "fontSize": 10.5, "leading": 13}),
    }


def pdf(data: list[dict], *, scope: str, sort: str, paper: str = "letter", orientation: str = "portrait",
        filters_text: str = "", generated: datetime | None = None, counter_line: bool = True) -> bytes:
    st = _styles()
    page = PAPERS.get(paper, PAPERS["letter"])[1]
    if orientation == "landscape":
        page = landscape(page)
    margin = 0.5 * inch
    usable = page[0] - 2 * margin
    generated = generated or datetime.now()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=0.55 * inch,
                            bottomMargin=0.6 * inch, title="Inventory reconciliation",
                            author=config.APP_NAME, subject=SCOPES.get(scope, scope))

    # column widths: ✓ | item | location | expected | counted | notes
    fixed = {"chk": 0.32 * inch, "loc": 1.1 * inch, "exp": 0.62 * inch, "cnt": 0.72 * inch}
    item_w = (usable - sum(fixed.values())) * 0.58
    notes_w = usable - sum(fixed.values()) - item_w
    widths = [fixed["chk"], item_w, fixed["loc"], fixed["exp"], fixed["cnt"], notes_w]

    s = summary(data)
    story = [
        Paragraph("Inventory reconciliation", st["title"]),
        Spacer(1, 3),
        Paragraph(escape(f"{SCOPES.get(scope, scope)} · {SORTS.get(sort, sort).split(' (')[0]}"
                         + (f" · {filters_text}" if filters_text else "")), st["meta"]),
        Paragraph(escape(f"Generated {generated:%Y-%m-%d %H:%M} · {s['items']} items · {s['units']} units · "
                         f"{s['locations']} locations" + (f" · {s['unplaced']} with no location" if s['unplaced'] else "")),
                  st["meta"]),
        Spacer(1, 6),
    ]
    if counter_line:
        sig = Table([["Counted by: ______________________________", "Date: ______________",
                      "Checked by: ______________________"]],
                    colWidths=[usable * 0.42, usable * 0.22, usable * 0.36])
        sig.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story += [sig, Spacer(1, 4)]
    story.append(Paragraph("Tick the box when the item is on its shelf. Write the actual count if it differs, and note damage, wrong shelf, "
                           "missing items or anything to fix in the listing.", st["sub"]))
    story.append(Spacer(1, 8))

    header = [Paragraph("OK", st["head"]), Paragraph("Item", st["head"]), Paragraph("Location", st["head"]),
              Paragraph("Expected", st["head"]), Paragraph("Counted", st["head"]), Paragraph("Notes", st["head"])]
    base_style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]

    def item_row(r):
        sub = []
        if r["sku"]:
            sub.append(escape(r["sku"]))
        if r["ebay_id"]:
            sub.append(f"eBay {r['ebay_id']}")
        if r["price"] is not None:
            sub.append(f"${r['price']:,.2f}")
        if r["status"] == "unlisted":
            sub.append("<b>not listed</b>")
        sub.append(f"#{r['id']}")
        item_cell = [Paragraph(escape(r["title"] or "—"), st["item"]), Paragraph(" · ".join(sub), st["sub"])]
        exp = str(r["qty"])
        if r["ebay_qty"] is not None and r["ebay_qty"] != r["qty"]:
            exp += f'<br/><font size="6.5" color="#555555">eBay {r["ebay_qty"]}</font>'
        loc_cell = Paragraph(escape(r["location"] or "—") + (f'<br/><font size="7" color="#555555">{r["code"]}</font>'
                                                             if r["code"] else ""), st["cell"])
        return [TickBox(), item_cell, loc_cell, Paragraph(exp, st["qty"]), "", ""]

    def block(rs, group=None, code=None):
        """One table: column header (+ group header), repeated if it runs onto the next page."""
        data, style = [header], list(base_style)
        repeat = 1
        if group is not None:
            n = len(rs)
            meta = f'{code + " · " if code else ""}{n} item{"s" if n != 1 else ""}'
            data.append([Paragraph(f'{escape(group)}  <font size="8" color="#555555">{meta}</font>', st["group"]),
                         "", "", "", "", ""])
            style += [("SPAN", (0, 1), (-1, 1)), ("BACKGROUND", (0, 1), (-1, 1), BAND), ("TOPPADDING", (0, 1), (-1, 1), 5)]
            repeat = 2
        for r in rs:
            data.append(item_row(r))
            i = len(data) - 1
            style += [("LINEBELOW", (0, i), (-1, i), 0.4, RULE), ("ALIGN", (0, i), (0, i), "CENTER"),
                      ("BOX", (4, i), (4, i), 0.6, RULE), ("LINEBEFORE", (5, i), (5, i), 0.4, RULE)]
        t = _GroupTable(data, colWidths=widths, repeatRows=repeat)
        t.setStyle(TableStyle(style))
        return t

    if not data:
        story.append(Paragraph("No items match these filters.", st["cell"]))
    elif sort == "title":
        story.append(block(data))
    else:
        groups: dict[str, list[dict]] = {}
        for r in data:
            groups.setdefault(r["location"] or NO_LOC, []).append(r)
        for g, rs in groups.items():
            code = next((x["code"] for x in rs if x["code"]), None)
            label = g if g != NO_LOC else f"{NO_LOC} — find these and assign a shelf"
            story.append(CondPageBreak(1.5 * inch))   # never start a shelf with no room for its first item
            story.append(block(rs, label, code))
            story.append(Spacer(1, 10))

    story.append(Spacer(1, 14))
    story.append(KeepTogether([
        Paragraph("Not on the list but found on a shelf", st["head"]), Spacer(1, 4),
        _blank_lines(usable, 5, [usable * 0.5, usable * 0.25, usable * 0.25], ["Item", "Location", "Qty"]),
    ]))

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(margin, 0.35 * inch, f"{config.APP_NAME} · Inventory reconciliation · {generated:%Y-%m-%d}")
        canvas.drawRightString(page[0] - margin, 0.35 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


class _GroupTable(Table):
    """Marks the repeated group header as "(continued)" on later pages."""

    def split(self, aw, ah):
        parts = super().split(aw, ah)
        for part in parts[1:]:
            if getattr(part, "repeatRows", 0) != 2:
                continue
            try:
                val = part._cellvalues[1][0]
                para = val[0] if isinstance(val, (tuple, list)) else val
                if isinstance(para, Paragraph) and "continued" not in para.text:
                    title = para.text.split("<font")[0].strip()
                    row = list(part._cellvalues[1])          # rows are shared between split parts — copy
                    row[0] = Paragraph(f'{title}  <font size="8" color="#555555">(continued)</font>', para.style)
                    part._cellvalues = [part._cellvalues[0], row] + list(part._cellvalues[2:])
            except (IndexError, AttributeError, TypeError):
                pass
        return parts


class TickBox(Flowable):
    """An empty square to tick by hand."""

    def __init__(self, size: float = 10):
        super().__init__()
        self.size = size

    def wrap(self, aw, ah):
        return self.size, self.size

    def draw(self):
        self.canv.setStrokeColor(INK)
        self.canv.setLineWidth(0.8)
        self.canv.rect(0, 0, self.size, self.size, stroke=1, fill=0)


def _blank_lines(width: float, n: int, cols: list[float], headers: list[str]) -> Table:
    data = [headers] + [[""] * len(cols) for _ in range(n)]
    t = Table(data, colWidths=cols, rowHeights=[14] + [22] * n)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                           ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    return t


def location_choices(db: Session) -> list[Location]:
    return db.query(Location).order_by(Location.name).all()
