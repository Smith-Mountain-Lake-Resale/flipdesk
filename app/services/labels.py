"""QR shelf-label layouts — HTML print view and exact-size PDF.

All sizes are in inches. 4×6 is portrait (4" wide, 6" tall), the way thermal label
printers (Rollo, Zebra, MUNBYN, Dymo 4XL…) feed shipping labels.
"""
from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, black
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

# key: (label, page_w, page_h, cols, rows, cell style)
LAYOUTS: dict[str, tuple] = {
    "4x6-4": ("4×6 label — 4 per label (2″×3″ each, cut in quarters)", 4.0, 6.0, 2, 2, "stack"),
    "4x6-6": ("4×6 label — 6 per label (2″×2″ each)", 4.0, 6.0, 2, 3, "stack"),
    "4x6-8": ("4×6 label — 8 per label (2″×1.5″ each, QR beside text)", 4.0, 6.0, 2, 4, "side"),
    "4x6-1": ("4×6 label — 1 per label (large)", 4.0, 6.0, 1, 1, "stack"),
    "4x2-1": ("4″×2″ label — 1 per label", 4.0, 2.0, 1, 1, "side"),
    "2.25x1.25-1": ("2.25″×1.25″ label — 1 per label", 2.25, 1.25, 1, 1, "side"),
    "2x1-1": ("2″×1″ label — 1 per label", 2.0, 1.0, 1, 1, "side"),
    "letter-30": ("Letter sheet — 30 per page (Avery 5160-style, 2.625″×1″)", 8.5, 11.0, 3, 10, "side"),
}
DEFAULT_LAYOUT = "4x6-4"

# Avery 5160 has page margins; everything else is edge to edge.
MARGINS = {"letter-30": (0.1875, 0.5)}   # (left, top) inches
GAPS = {"letter-30": (0.125, 0.0)}       # (horizontal, vertical) inches between labels


def layout(key: str) -> dict:
    name, pw, ph, cols, rows, style = LAYOUTS.get(key) or LAYOUTS[DEFAULT_LAYOUT]
    ml, mt = MARGINS.get(key, (0.0, 0.0))
    gx, gy = GAPS.get(key, (0.0, 0.0))
    cw = (pw - 2 * ml - gx * (cols - 1)) / cols
    ch = (ph - 2 * mt - gy * (rows - 1)) / rows
    pad = 0.12 if min(cw, ch) >= 1.4 else 0.07
    if style == "stack":
        qr = min(cw - 2 * pad, ch * 0.62)
    else:
        qr = min(ch - 2 * pad, cw * 0.47)
    return {"key": key if key in LAYOUTS else DEFAULT_LAYOUT, "name": name, "pw": pw, "ph": ph, "cols": cols,
            "rows": rows, "per_page": cols * rows, "style": style, "cw": cw, "ch": ch, "ml": ml, "mt": mt,
            "gx": gx, "gy": gy, "pad": pad, "qr": round(qr, 3), "cut_lines": cols * rows > 1 and key != "letter-30"}


def expand(labels: list[dict], copies: int) -> list[dict]:
    out = []
    for lab in labels:
        out.extend([lab] * max(1, min(copies, 50)))
    return out


def pages(labels: list[dict], per_page: int, skip: int = 0) -> list[list[dict | None]]:
    """Split into pages. `skip` leaves the first N cells blank (to reuse a partly used sheet)."""
    cells: list[dict | None] = [None] * max(0, min(skip, per_page - 1)) + labels
    return [cells[i:i + per_page] + [None] * (per_page - len(cells[i:i + per_page]))
            for i in range(0, max(len(cells), 1), per_page)]


ALNUM = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:")


def compact(url: str) -> str:
    """Upper-case the label URL when every character fits QR alphanumeric mode.
    Scheme and host are case-insensitive and /L/ is routed like /l/, so the link
    still works — but the code needs fewer, larger modules (easier to scan small)."""
    up = url.upper()
    return up if set(up) <= ALNUM else url


def qr_svg(data: str) -> str:
    data = compact(data)
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf)
    s = buf.getvalue().decode()
    return s[s.find("<svg"):]


def _wrap(text: str, font: str, size: float, width_pt: float) -> tuple[list[str], bool]:
    """Greedy word wrap. Returns (lines, had_to_split_a_word)."""
    words, lines, cur, split = text.split(), [], "", False
    for w in words:
        trial = (cur + " " + w).strip()
        if stringWidth(trial, font, size) <= width_pt:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while stringWidth(w, font, size) > width_pt and len(w) > 1:
            split = True
            cut = len(w)
            while cut > 1 and stringWidth(w[:cut], font, size) > width_pt:
                cut -= 1
            lines.append(w[:cut])
            w = w[cut:]
        cur = w
    if cur:
        lines.append(cur)
    return (lines or [""]), split


def text_box(lay: dict) -> tuple[float, float]:
    """(width, height) in inches available for text in one cell."""
    if lay["style"] == "stack":
        return lay["cw"] - 2 * lay["pad"], lay["ch"] - lay["qr"] - 2.6 * lay["pad"]
    return lay["cw"] - lay["qr"] - 3 * lay["pad"], lay["ch"] - 2 * lay["pad"]


LEAD = 1.12      # name line height (× font size)
CODE_LEAD = 1.4  # code line incl. gap above


def sizes_for(lay: dict, name: str, sub: str = "") -> dict:
    """Largest name font where the whole name, the code (and description) fit the cell,
    without splitting words. Same numbers drive the PDF and the HTML print view."""
    tw, th = text_box(lay)
    avail_h = th * 72
    small = min(lay["cw"], lay["ch"]) < 1.4
    min_code = 6.0 if small else 8.0
    max_lines = 3 if lay["style"] == "side" else 2
    start = min(40.0 if lay["per_page"] == 1 else 26.0, avail_h * 0.6)
    size = start
    best = None
    while size >= 6:
        lines, split = _wrap(name, "Helvetica-Bold", size, tw * inch)
        code_pt = max(min_code, min(14.0, size * 0.5))
        show_sub = bool(sub) and lay["style"] == "stack"
        total = len(lines) * size * LEAD + code_pt * CODE_LEAD + (code_pt * 1.25 if show_sub else 0)
        if total <= avail_h and len(lines) <= max_lines and not split:
            return {"name_pt": size, "code_pt": round(code_pt, 1), "lines": lines, "tw": tw, "th": th, "show_sub": show_sub}
        if best is None and total <= avail_h and len(lines) <= max_lines + 1:
            best = (size, lines, code_pt, show_sub)
        size -= 0.5
    # last resort (very long names on tiny labels): smallest size, ellipsis after the last line
    lines, _ = _wrap(name, "Helvetica-Bold", 6, tw * inch)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()[:-1] + "…"
    return {"name_pt": 6.0, "code_pt": min_code, "lines": lines, "tw": tw, "th": th, "show_sub": False}


# ------------------------------------------------------------------ PDF
def pdf(labels: list[dict], key: str, skip: int = 0, cut_lines: bool = True) -> bytes:
    lay = layout(key)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(lay["pw"] * inch, lay["ph"] * inch))
    c.setTitle("FlipDesk shelf labels")
    for page in pages(labels, lay["per_page"], skip):
        if cut_lines and lay["cut_lines"]:
            c.setStrokeColor(Color(0.55, 0.55, 0.55))
            c.setLineWidth(0.4)
            c.setDash(3, 3)
            for i in range(1, lay["cols"]):
                x = (lay["ml"] + i * lay["cw"]) * inch
                c.line(x, 0, x, lay["ph"] * inch)
            for j in range(1, lay["rows"]):
                y = (lay["ph"] - lay["mt"] - j * lay["ch"]) * inch
                c.line(0, y, lay["pw"] * inch, y)
            c.setDash()
        for idx, lab in enumerate(page):
            if not lab:
                continue
            col, row = idx % lay["cols"], idx // lay["cols"]
            x0 = lay["ml"] + col * (lay["cw"] + lay["gx"])
            y_top = lay["ph"] - lay["mt"] - row * (lay["ch"] + lay["gy"])
            _cell(c, lay, lab, x0, y_top)
        c.showPage()
    c.save()
    return buf.getvalue()


def _cell(c: canvas.Canvas, lay: dict, lab: dict, x0: float, y_top: float) -> None:
    pad, q = lay["pad"], lay["qr"]
    sz = sizes_for(lay, lab["name"], lab.get("sub") or "")
    lines = sz["lines"]
    if lay["style"] == "stack":
        qx = x0 + (lay["cw"] - q) / 2
        qy = y_top - pad - q
        _qr(c, lab["url"], qx, qy, q)
        cx = x0 + lay["cw"] / 2
        y = qy - pad * 0.6
        c.setFillColor(black)
        for ln in lines:
            y -= sz["name_pt"] / 72
            c.setFont("Helvetica-Bold", sz["name_pt"])
            c.drawCentredString(cx * inch, y * inch, ln)
            y -= sz["name_pt"] * (LEAD - 1) / 72
        y -= sz["code_pt"] * CODE_LEAD / 72
        c.setFont("Courier-Bold", sz["code_pt"])
        c.drawCentredString(cx * inch, y * inch, lab["code"])
        if sz["show_sub"]:
            c.setFont("Helvetica", max(6, sz["code_pt"] * 0.85))
            c.drawCentredString(cx * inch, (y - sz["code_pt"] * 1.25 / 72) * inch, lab["sub"][:60])
    else:
        qx = x0 + pad
        qy = y_top - (lay["ch"] + q) / 2
        _qr(c, lab["url"], qx, qy, q)
        tx = qx + q + pad
        block = len(lines) * sz["name_pt"] * LEAD + sz["code_pt"] * CODE_LEAD
        y = y_top - lay["ch"] / 2 + block / 2 / 72
        c.setFillColor(black)
        for ln in lines:
            y -= sz["name_pt"] / 72
            c.setFont("Helvetica-Bold", sz["name_pt"])
            c.drawString(tx * inch, y * inch, ln)
            y -= sz["name_pt"] * (LEAD - 1) / 72
        y -= sz["code_pt"] * CODE_LEAD / 72
        c.setFont("Courier-Bold", sz["code_pt"])
        c.drawString(tx * inch, y * inch, lab["code"])


def _qr(c: canvas.Canvas, data: str, x: float, y: float, size: float) -> None:
    w = QrCodeWidget(compact(data), barLevel="M", barBorder=1)
    b = w.getBounds()
    bw, bh = b[2] - b[0], b[3] - b[1]
    d = Drawing(size * inch, size * inch, transform=[size * inch / bw, 0, 0, size * inch / bh, 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x * inch, y * inch)
