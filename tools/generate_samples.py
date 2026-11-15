"""Build the test invoices into data/samples/.

Two steps per invoice: render a clean PDF with reportlab, then (Phase 3+)
optionally degrade it into a scan or photograph. Deterministic: reportlab runs
in invariant mode (no timestamps or random document IDs), so the same source
produces byte-identical files and the LLM cache, keyed by sha256, stays valid.

The generator computes the printed figures (it is the vendor's billing system
in this story); the pipeline never sees these numbers except by reading the page.

Usage:  python -m tools.generate_samples
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app import config

W, H = A4
M = 36  # page margin, pt
TWO = Decimal("0.01")


# ---------------------------------------------------------------- document model

@dataclass
class VendorStyle:
    name: str
    address: list[str]
    gstin: str
    pan: str
    phone: str
    email: str
    website: str
    color: str          # brand colour, hex
    monogram: str
    state: str


@dataclass
class Line:
    sku: str
    description: str
    hsn: str
    qty: Decimal
    uom: str
    rate: Decimal
    gst_pct: Decimal

    @property
    def amount(self) -> Decimal:
        return (self.qty * self.rate).quantize(TWO, ROUND_HALF_UP)


@dataclass
class InvoiceDoc:
    filename: str
    vendor: VendorStyle
    invoice_no: str
    invoice_date: str           # exactly as printed
    due_date: str
    po_ref: str | None          # printed in the header strip, or None
    place_of_supply: str
    lines: list[Line]
    tax_mode: str               # "intra" (CGST+SGST) | "inter" (IGST)
    freight: Decimal
    bank: dict[str, str]
    terms: str
    notes: list[str] = field(default_factory=list)
    currency_label: str = "INR"

    @property
    def subtotal(self) -> Decimal:
        return sum((l.amount for l in self.lines), Decimal("0"))

    def taxes(self) -> list[tuple[str, Decimal]]:
        by_rate: dict[Decimal, Decimal] = {}
        for l in self.lines:
            by_rate[l.gst_pct] = by_rate.get(l.gst_pct, Decimal("0")) + l.amount
        out = []
        for rate, base in sorted(by_rate.items()):
            if self.tax_mode == "intra":
                half = rate / 2
                amt = (base * half / 100).quantize(TWO, ROUND_HALF_UP)
                out += [(f"CGST @ {half.normalize()}%", amt), (f"SGST @ {half.normalize()}%", amt)]
            else:
                out.append((f"IGST @ {rate.normalize()}%", (base * rate / 100).quantize(TWO, ROUND_HALF_UP)))
        return out

    @property
    def grand_total(self) -> Decimal:
        return self.subtotal + sum((a for _, a in self.taxes()), Decimal("0")) + self.freight


# ---------------------------------------------------------------- formatting

def inr(d: Decimal) -> str:
    """Indian digit grouping: 1,23,456.78"""
    whole, frac = f"{d:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{whole}.{frac}"


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    return _ONES[n] if n < 20 else _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")


def _three(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n:
        parts.append(_two(n))
    return " ".join(parts)


def words_indian(d: Decimal) -> str:
    """95250.00 -> 'Rupees Ninety-Five Thousand Two Hundred Fifty Only' (lakh/crore system)."""
    rupees, paise = int(d), int((d - int(d)) * 100)
    parts, n = [], rupees
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if n >= size:
            parts.append(f"{_three(n // size) if n // size < 1000 else words_indian(Decimal(n // size))} {name}")
            n %= size
    if n:
        parts.append(_three(n))
    text = "Rupees " + " ".join(parts)
    if paise:
        text += f" and {_two(paise)} Paise"
    return text + " Only"


# ---------------------------------------------------------------- rendering

def _hex(c: str) -> colors.Color:
    return colors.HexColor(c)


INK = _hex("#1f2328")
MUTED = _hex("#5b6470")
RULE = _hex("#9aa3ad")
SHADE = _hex("#f1f3f5")


def _logo(c: canvas.Canvas, x: float, y: float, style: VendorStyle) -> None:
    """A drawn mark: filled disc, an offset ring segment, and a monogram."""
    brand = _hex(style.color)
    c.saveState()
    c.setFillColor(brand)
    c.circle(x + 22, y + 22, 22, stroke=0, fill=1)
    c.setStrokeColor(colors.white)
    c.setLineWidth(2.2)
    c.arc(x + 7, y + 7, x + 37, y + 37, startAng=200, extent=230)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(x + 22, y + 17.5, style.monogram)
    c.restoreState()


def _signature(c: canvas.Canvas, x: float, y: float) -> None:
    c.saveState()
    c.setStrokeColor(_hex("#1d3a8a"))
    c.setLineWidth(1.1)
    p = c.beginPath()
    p.moveTo(x, y)
    p.curveTo(x + 10, y + 18, x + 18, y - 6, x + 28, y + 8)
    p.curveTo(x + 36, y + 20, x + 40, y - 4, x + 52, y + 6)
    p.curveTo(x + 60, y + 12, x + 66, y + 2, x + 80, y + 4)
    c.drawPath(p, stroke=1, fill=0)
    c.restoreState()


def _box(c: canvas.Canvas, x: float, y: float, w: float, h: float, fill: colors.Color | None = None) -> None:
    c.saveState()
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    if fill is not None:
        c.setFillColor(fill)
    c.rect(x, y, w, h, stroke=1, fill=1 if fill is not None else 0)
    c.restoreState()


def _label_value(c: canvas.Canvas, x: float, y: float, label: str, value: str, bold: bool = True) -> None:
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(x, y + 10, label.upper())
    c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5)
    c.setFillColor(INK)
    c.drawString(x, y, value)


def render_pdf(doc: InvoiceDoc) -> bytes:
    """Render the digital original: a real text layer, vector graphics."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle(f"Tax Invoice {doc.invoice_no}")
    c.setAuthor(doc.vendor.name)
    v, brand = doc.vendor, _hex(doc.vendor.color)

    # Letterhead
    top = H - M
    _logo(c, M, top - 46, v)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(M + 56, top - 16, v.name)
    c.setFont("Helvetica", 8)
    c.setFillColor(MUTED)
    for i, line in enumerate(v.address + [f"Tel {v.phone}  |  {v.email}  |  {v.website}"]):
        c.drawString(M + 56, top - 28 - i * 10, line)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(M + 56, top - 28 - (len(v.address) + 1) * 10, f"GSTIN: {v.gstin}    PAN: {v.pan}")

    c.setFillColor(brand)
    c.setFont("Helvetica-Bold", 18)
    c.drawRightString(W - M, top - 16, "TAX INVOICE")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(MUTED)
    c.drawRightString(W - M, top - 28, "ORIGINAL FOR RECIPIENT")
    c.setStrokeColor(brand)
    c.setLineWidth(2)
    y = top - 40 - (len(v.address) + 1) * 10
    c.line(M, y, W - M, y)

    # Invoice strip
    y -= 38
    cells = [("Invoice No.", doc.invoice_no), ("Invoice Date", doc.invoice_date),
             ("PO Reference", doc.po_ref or "-"), ("Due Date", doc.due_date),
             ("Place of Supply", doc.place_of_supply)]
    cw = (W - 2 * M) / len(cells)
    for i, (label, value) in enumerate(cells):
        _box(c, M + i * cw, y - 6, cw, 30)
        _label_value(c, M + i * cw + 6, y + 2, label, value)

    # Bill To / Ship To
    y -= 16
    half = (W - 2 * M - 12) / 2
    for i, (title, lines_, gst) in enumerate([("Bill To", config.BUYER["bill_to"], config.BUYER["gstin"]),
                                              ("Ship To", config.BUYER["ship_to"], None)]):
        x = M + i * (half + 12)
        _box(c, x, y - 78, half, 74)
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(brand)
        c.drawString(x + 8, y - 16, title.upper())
        c.setFillColor(INK)
        for j, line in enumerate(lines_):
            c.setFont("Helvetica-Bold" if j == 0 else "Helvetica", 8.5 if j == 0 else 8)
            c.drawString(x + 8, y - 29 - j * 10, line)
        if gst:
            c.setFont("Helvetica", 8)
            c.drawString(x + 8, y - 29 - len(lines_) * 10, f"GSTIN: {gst}")

    # Line-item table
    y -= 98
    cols = [("Sr", 20, "c"), ("Item Code", 52, "l"), ("Description", 165, "l"), ("HSN", 38, "c"),
            ("Qty", 36, "r"), ("UOM", 32, "c"), ("Rate", 58, "r"), ("GST %", 36, "r"), ("Amount", 86, "r")]
    scale = (W - 2 * M) / sum(w for _, w, _ in cols)
    cols = [(n, w * scale, a) for n, w, a in cols]
    row_h = 20

    def cell(text: str, x: float, w: float, align: str, yy: float, font: str = "Helvetica", size: float = 8.5):
        c.setFont(font, size)
        if align == "l":
            c.drawString(x + 5, yy, text)
        elif align == "r":
            c.drawRightString(x + w - 5, yy, text)
        else:
            c.drawCentredString(x + w / 2, yy, text)

    _box(c, M, y - row_h, W - 2 * M, row_h, fill=brand)
    c.setFillColor(colors.white)
    x = M
    for name, w, a in cols:
        cell(name, x, w, a, y - 13.5, "Helvetica-Bold", 8)
        x += w
    y -= row_h
    for i, l in enumerate(doc.lines, start=1):
        _box(c, M, y - row_h, W - 2 * M, row_h, fill=SHADE if i % 2 == 0 else colors.white)
        c.setFillColor(INK)
        values = [str(i), l.sku, l.description, l.hsn, f"{l.qty.normalize():f}", l.uom, inr(l.rate),
                  f"{l.gst_pct.normalize()}%", inr(l.amount)]
        x = M
        for (name, w, a), val in zip(cols, values):
            cell(val, x, w, a, y - 13.5)
            x += w
        y -= row_h
    # vertical rules
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    x = M
    table_top = y + row_h * (len(doc.lines) + 1)
    for _, w, _ in cols[:-1]:
        x += w
        c.line(x, table_top, x, y)

    # Totals, right-aligned
    y -= 12
    tx, tw = W - M - 220, 220
    rows = [("Taxable Value", inr(doc.subtotal))] + [(lbl, inr(a)) for lbl, a in doc.taxes()]
    if doc.freight:
        rows.append(("Freight & Forwarding", inr(doc.freight)))
    for lbl, val in rows:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(MUTED)
        c.drawString(tx + 6, y - 12, lbl)
        c.setFillColor(INK)
        c.drawRightString(tx + tw - 6, y - 12, val)
        y -= 16
    _box(c, tx, y - 22, tw, 24, fill=_hex("#eef2f7"))
    c.setFont("Helvetica-Bold", 10.5)
    c.setFillColor(INK)
    c.drawString(tx + 6, y - 14, f"Grand Total ({doc.currency_label})")
    c.drawRightString(tx + tw - 6, y - 14, inr(doc.grand_total))
    totals_bottom = y - 22

    # Amount in words (left of totals)
    wy = table_top - row_h * (len(doc.lines) + 1) - 24
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(M, wy, "AMOUNT IN WORDS")
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(INK)
    words = words_indian(doc.grand_total)
    c.drawString(M, wy - 12, words[:62])
    if len(words) > 62:
        c.drawString(M, wy - 23, words[62:])

    # Bank details + terms
    y = totals_bottom - 26
    _box(c, M, y - 84, half, 80)
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(brand)
    c.drawString(M + 8, y - 16, "BANK DETAILS FOR PAYMENT")
    c.setFillColor(INK)
    for j, (k, val) in enumerate(doc.bank.items()):
        c.setFont("Helvetica", 8)
        c.drawString(M + 8, y - 30 - j * 11, f"{k}:")
        c.setFont("Helvetica-Bold", 8)
        c.drawString(M + 78, y - 30 - j * 11, val)
    bx = M + half + 12
    _box(c, bx, y - 84, half, 80)
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(brand)
    c.drawString(bx + 8, y - 16, "TERMS")
    c.setFillColor(INK)
    c.setFont("Helvetica", 8)
    for j, t in enumerate([doc.terms, *doc.notes]):
        c.drawString(bx + 8, y - 30 - j * 11, t)

    # Declaration + signature
    y -= 110
    c.setFont("Helvetica", 7)
    c.setFillColor(MUTED)
    c.drawString(M, y, "Declaration: We declare that this invoice shows the actual price of the goods described")
    c.drawString(M, y - 9, "and that all particulars are true and correct.")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawRightString(W - M, y, f"For {v.name}")
    _signature(c, W - M - 110, y - 30)
    c.setStrokeColor(RULE)
    c.line(W - M - 140, y - 40, W - M, y - 40)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(W - M, y - 50, "Authorised Signatory")

    # Footer
    c.setFont("Helvetica", 6.5)
    c.setFillColor(MUTED)
    c.drawCentredString(W / 2, M - 6, f"Subject to {v.state} jurisdiction  |  This is a computer-generated invoice  "
                                      "|  Page 1 of 1")
    c.showPage()
    c.save()
    return buf.getvalue()


def as_clean_pdf(doc: InvoiceDoc) -> bytes:
    """The digital original, text layer intact."""
    return render_pdf(doc)


# ---------------------------------------------------------------- the samples

NORDIC = VendorStyle(
    name="Nordic Components Pvt Ltd",
    address=["No. 42, 2nd Main Road, Peenya Industrial Area Phase II", "Bengaluru 560058, Karnataka, India"],
    gstin="29AABCN4471K1Z3", pan="AABCN4471K", phone="+91 80 4127 6650",
    email="accounts@nordiccomponents.in", website="www.nordiccomponents.in",
    color="#0f4c5c", monogram="NC", state="Bengaluru")

NORDIC_BANK = {"Bank": "HDFC Bank Ltd, Peenya Branch", "A/c Name": "Nordic Components Pvt Ltd",
               "A/c No.": "50200012344471", "IFSC": "HDFC0000523"}


def happy_path() -> InvoiceDoc:
    return InvoiceDoc(
        filename="HP_nordic_clean.pdf", vendor=NORDIC,
        invoice_no="INV-NC-5531", invoice_date="02/11/2026", due_date="02/12/2026", po_ref="PO-2041",
        place_of_supply="Karnataka (29)",
        lines=[Line("BRG-12", "Deep groove ball bearing 6204-2RS", "8482", Decimal(60), "NOS", Decimal("750.00"),
                    Decimal(18)),
               Line("SHF-20", "Precision ground shaft 20mm x 300mm", "8483", Decimal(20), "NOS",
                    Decimal("1500.00"), Decimal(18)),
               Line("SEAL-8", "Nitrile oil seal 35x52x8", "4016", Decimal(100), "NOS", Decimal("50.00"),
                    Decimal(18))],
        tax_mode="intra", freight=Decimal("850.00"), bank=NORDIC_BANK,
        terms="Payment: Net 30 days from invoice date",
        notes=["Goods dispatched via Safexpress, LR 88213047", "Interest @18% p.a. on overdue payments"])


SAMPLES = [(happy_path, as_clean_pdf)]


def generate(out_dir: Path | None = None) -> list[Path]:
    out = Path(out_dir or config.SAMPLES_DIR)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for build, render in SAMPLES:
        doc = build()
        path = out / doc.filename
        path.write_bytes(render(doc))
        written.append(path)
    return written


def main() -> None:
    for p in generate():
        print(f"  wrote {p.relative_to(config.ROOT_DIR)} ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
