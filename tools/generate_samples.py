"""Build the test invoices into data/samples/.

Two steps per invoice: render a clean PDF with reportlab, then optionally
degrade it into a scan, a fax or a phone photograph with the named, composable
degraders below (as_scanned_pdf, as_png, as_jpeg_photo, as_tiff, degrade).

Deterministic: reportlab runs in invariant mode (no timestamps or random
document IDs) and every degrader draws from an RNG seeded by the filename, so
the same source produces byte-identical files and the LLM cache, keyed by
sha256, stays valid.

The generator computes the printed figures (it is the vendor's billing system
in this story); the pipeline never sees these numbers except by reading the page.

Usage:  python -m tools.generate_samples
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
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
    layout: str = "standard"    # standard (letterhead + strip) | two_column | european


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

    if v.layout == "two_column":
        y = _header_two_column(c, doc)
    else:
        y = _header_standard(c, doc)

    # Bill To / Ship To
    y -= 16
    half = (W - 2 * M - 12) / 2
    _parties(c, y, half, brand)
    y = _body(c, doc, y, half, brand)
    c.showPage()
    c.save()
    return buf.getvalue()


def _header_standard(c: canvas.Canvas, doc: InvoiceDoc) -> float:
    """V001-style: letterhead across the top, then a five-cell invoice strip."""
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

    # Invoice strip (a vendor that quotes the PO in its notes has no PO cell)
    y -= 38
    cells = [("Invoice No.", doc.invoice_no), ("Invoice Date", doc.invoice_date),
             ("PO Reference", doc.po_ref or "-"), ("Due Date", doc.due_date),
             ("Place of Supply", doc.place_of_supply)]
    if doc.po_ref is None:
        cells = [cell for cell in cells if cell[0] != "PO Reference"]
    cw = (W - 2 * M) / len(cells)
    for i, (label, value) in enumerate(cells):
        _box(c, M + i * cw, y - 6, cw, 30)
        _label_value(c, M + i * cw + 6, y + 2, label, value)
    return y


def _header_two_column(c: canvas.Canvas, doc: InvoiceDoc) -> float:
    """V002/V004-style: supplier block left, an invoice-details panel right."""
    v, brand = doc.vendor, _hex(doc.vendor.color)
    top = H - M
    c.setFillColor(brand)
    c.rect(M, top - 4, W - 2 * M, 4, stroke=0, fill=1)
    _logo(c, M, top - 60, v)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(M + 54, top - 26, v.name)
    lines = v.address + [f"Tel {v.phone}", f"{v.email}  |  {v.website}"]
    c.setFont("Helvetica", 7.8)
    c.setFillColor(MUTED)
    for i, line in enumerate(lines):
        c.drawString(M + 54, top - 38 - i * 9.5, line)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(INK)
    left_bottom = top - 38 - len(lines) * 9.5
    c.drawString(M + 54, left_bottom, f"GSTIN: {v.gstin}    PAN: {v.pan}")

    bx, bw = W - M - 200, 200
    c.setFillColor(brand)
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(W - M, top - 24, "TAX INVOICE")
    rows = [("Invoice No.", doc.invoice_no), ("Invoice Date", doc.invoice_date), ("Due Date", doc.due_date)]
    if doc.po_ref:
        rows.append(("Buyer's PO No.", doc.po_ref))
    rows.append(("Place of Supply", doc.place_of_supply))
    y = top - 34
    box_h = len(rows) * 15 + 6
    _box(c, bx, y - box_h, bw, box_h, fill=SHADE)
    for i, (k, val) in enumerate(rows):
        yy = y - 13 - i * 15
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawString(bx + 8, yy, k)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(INK)
        c.drawRightString(bx + bw - 8, yy, val)
    bottom = min(left_bottom, y - box_h) - 12
    c.setStrokeColor(brand)
    c.setLineWidth(1.2)
    c.line(M, bottom, W - M, bottom)
    return bottom + 4


def _parties(c: canvas.Canvas, y: float, half: float, brand: colors.Color) -> None:
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


def _body(c: canvas.Canvas, doc: InvoiceDoc, y: float, half: float, brand: colors.Color) -> float:
    v = doc.vendor
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
    return y


def eur(d: Decimal) -> str:
    """European grouping: 6.980,00"""
    return f"{d:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render_pdf_eu(doc: InvoiceDoc) -> bytes:
    """V006-style European invoice: sender line, address window, a details
    block on the right, Pos./Article/HS columns, VAT-free export, IBAN/BIC."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle(f"Invoice {doc.invoice_no}")
    c.setAuthor(doc.vendor.name)
    v, brand = doc.vendor, _hex(doc.vendor.color)
    top = H - M

    _logo(c, W - M - 44, top - 44, v)
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(INK)
    c.drawRightString(W - M - 52, top - 16, v.name)
    c.setFont("Helvetica", 7.8)
    c.setFillColor(MUTED)
    for i, line in enumerate(v.address + [v.phone, v.email]):
        c.drawRightString(W - M - 52, top - 28 - i * 9.5, line)

    # Sender line + address window (DIN 5008 style)
    c.setFont("Helvetica", 6.5)
    c.setFillColor(MUTED)
    c.drawString(M, top - 92, f"{v.name} - {v.address[0]} - {v.address[1]}")
    c.setStrokeColor(RULE)
    c.setLineWidth(0.4)
    c.line(M, top - 95, M + 230, top - 95)
    c.setFillColor(INK)
    for j, line in enumerate(config.BUYER["bill_to"] + [f"GSTIN {config.BUYER['gstin']}"]):
        c.setFont("Helvetica-Bold" if j == 0 else "Helvetica", 9 if j == 0 else 8.5)
        c.drawString(M, top - 110 - j * 11, line)

    # Details block
    rows = [("Invoice no.", doc.invoice_no), ("Invoice date", doc.invoice_date),
            ("Your order", doc.po_ref or "-"), ("Customer no.", "K-10482"), ("Due date", doc.due_date),
            ("VAT ID", v.gstin)]
    bx = W - M - 190
    for i, (k, val) in enumerate(rows):
        yy = top - 104 - i * 13
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawString(bx, yy, k)
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(INK)
        c.drawRightString(W - M, yy, val)

    y = top - 196
    c.setFont("Helvetica-Bold", 17)
    c.setFillColor(brand)
    c.drawString(M, y, f"Invoice {doc.invoice_no}")
    c.setFont("Helvetica", 8.5)
    c.setFillColor(INK)
    c.drawString(M, y - 16, "We thank you for your order and invoice the following deliveries:")

    # Table
    y -= 34
    cols = [("Pos.", 30, "c"), ("Article", 62, "l"), ("Description", 175, "l"), ("HS code", 52, "c"),
            ("Qty", 36, "r"), ("Unit", 32, "c"), ("Unit price", 62, "r"), ("Amount EUR", 74, "r")]
    scale = (W - 2 * M) / sum(w for _, w, _ in cols)
    cols = [(n, w * scale, a) for n, w, a in cols]
    row_h = 19

    def cell(text: str, x: float, w: float, align: str, yy: float, font: str = "Helvetica", size: float = 8.5):
        c.setFont(font, size)
        if align == "l":
            c.drawString(x + 5, yy, text)
        elif align == "r":
            c.drawRightString(x + w - 5, yy, text)
        else:
            c.drawCentredString(x + w / 2, yy, text)

    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.line(M, y, W - M, y)
    c.setFillColor(INK)
    x = M
    for name, w, a in cols:
        cell(name, x, w, a, y - 13, "Helvetica-Bold", 8)
        x += w
    y -= row_h
    c.line(M, y, W - M, y)
    for i, l in enumerate(doc.lines, start=1):
        if i % 2 == 0:
            c.setFillColor(SHADE)
            c.rect(M, y - row_h, W - 2 * M, row_h, stroke=0, fill=1)
        c.setFillColor(INK)
        values = [f"{i * 10}", l.sku, l.description, l.hsn, f"{l.qty.normalize():f}", l.uom, eur(l.rate), eur(l.amount)]
        x = M
        for (name, w, a), val in zip(cols, values):
            cell(val, x, w, a, y - 13)
            x += w
        y -= row_h
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(M, y, W - M, y)

    # Totals
    y -= 10
    tx, tw = W - M - 230, 230
    for lbl, val in [("Net amount", eur(doc.subtotal)),
                     ("VAT 0% - tax-free export delivery (s. 4 No. 1a UStG)", eur(Decimal("0.00")))]:
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawString(tx, y - 12, lbl)
        c.setFillColor(INK)
        c.drawRightString(tx + tw, y - 12, val)
        y -= 15
    c.setStrokeColor(INK)
    c.line(tx, y - 4, tx + tw, y - 4)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(tx, y - 18, "Total amount EUR")
    c.drawRightString(tx + tw, y - 18, eur(doc.grand_total))
    c.line(tx, y - 24, tx + tw, y - 24)
    c.line(tx, y - 26, tx + tw, y - 26)
    y -= 50

    c.setFont("Helvetica", 8.5)
    c.setFillColor(INK)
    for j, t in enumerate([doc.terms, *doc.notes]):
        c.drawString(M, y - j * 12, t)
    y -= 12 * (len(doc.notes) + 1) + 12

    # Signature
    c.setFont("Helvetica", 8.5)
    c.drawString(M, y, "Kind regards,")
    _signature(c, M, y - 26)
    c.drawString(M, y - 40, v.name)

    # Footer: bank and registry, three columns
    fy = M + 34
    c.setStrokeColor(brand)
    c.setLineWidth(1)
    c.line(M, fy + 12, W - M, fy + 12)
    colw = (W - 2 * M) / 3
    blocks = [[v.name, *v.address],
              ["Bank details", *[f"{k}: {val}" for k, val in doc.bank.items()]],
              ["Managing director: K. Brandt", "Register court Stuttgart HRB 74311", f"VAT ID {v.gstin}"]]
    for i, block in enumerate(blocks):
        for j, line in enumerate(block):
            c.setFont("Helvetica-Bold" if j == 0 else "Helvetica", 6.8)
            c.setFillColor(INK if j == 0 else MUTED)
            c.drawString(M + i * colw, fy - j * 8.5, line)
    c.showPage()
    c.save()
    return buf.getvalue()


def _render(doc: InvoiceDoc) -> bytes:
    return render_pdf_eu(doc) if doc.vendor.layout == "european" else render_pdf(doc)


def as_clean_pdf(doc: InvoiceDoc) -> bytes:
    """The digital original, text layer intact."""
    return _render(doc)


# ---------------------------------------------------------------- degraders
#
# Each takes a document and returns file bytes. They share one RNG per
# document (seeded by its filename) so every run produces the same file.

SCAN_DPI = 200
# A phone photo of a page at arm's length resolves about 120 DPI of paper: the
# measured tesseract legibility is ~0.69, a degraded read that stays above the
# 0.55 floor (so Edge 1 reaches AUTO_APPROVE once the clerk supplies the number).
PHOTO_DPI = 120


def _rng(doc: InvoiceDoc) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha256(doc.filename.encode()).hexdigest()[:8], 16))


def _raster(doc: InvoiceDoc, dpi: int = SCAN_DPI) -> Image.Image:
    pdf = pymupdf.open(stream=_render(doc), filetype="pdf")
    zoom = dpi / 72.0
    pix = pdf[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _text_rect_px(doc: InvoiceDoc, text: str, dpi: int = SCAN_DPI) -> tuple[int, int, int, int]:
    """Where `text` sits on the rendered page, in pixels at `dpi`."""
    pdf = pymupdf.open(stream=_render(doc), filetype="pdf")
    hits = pdf[0].search_for(text)
    if not hits:
        raise ValueError(f"{text!r} is not printed on {doc.filename}")
    r, k = hits[0], dpi / 72.0
    return int(r.x0 * k), int(r.y0 * k), int(r.x1 * k), int(r.y1 * k)


def degrade(img: Image.Image, level: str = "medium", rng: np.random.Generator | None = None) -> Image.Image:
    """Scanner wear, scaled by level: slight rotation, gaussian noise, JPEG
    recompression artefacts, 1-2 light horizontal streaks, 2% contrast loss and
    a faint diagonal COPY watermark at 6% opacity."""
    rng = rng or np.random.default_rng(0)
    k = {"light": 0.5, "medium": 1.0, "heavy": 1.6}[level]
    gray = img.convert("L")
    w, h = gray.size

    # Faint diagonal watermark
    mark = Image.new("L", (w, h), 0)
    font = ImageFont.load_default(size=int(h * 0.16))
    ImageDraw.Draw(mark).text((w * 0.14, h * 0.36), "COPY", fill=255, font=font)
    mark = mark.rotate(32, resample=Image.Resampling.BICUBIC, center=(w / 2, h / 2))
    a = np.asarray(gray, dtype=np.float32)
    a = a - (np.asarray(mark, dtype=np.float32) / 255.0) * 0.06 * a

    # 2% contrast loss, then light scanner streaks
    a = 128 + (a - 128) * 0.98
    for _ in range(1 + int(rng.integers(0, 2))):
        row = int(rng.integers(int(h * 0.1), int(h * 0.9)))
        a[row:row + 2, :] = a[row:row + 2, :] * 0.55 + 255 * 0.45

    # Gaussian noise
    a = a + rng.normal(0, 3.0 * k, a.shape)
    out = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L")

    # Slight rotation (the page sat crooked on the glass)
    angle = float(rng.uniform(0.35, 0.6)) * k * (1 if rng.random() < 0.5 else -1)
    out = out.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=250)

    # JPEG recompression artefacts
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=int(88 - 18 * k))
    return Image.open(io.BytesIO(buf.getvalue())).convert("L")


def as_scanned_pdf(doc: InvoiceDoc) -> bytes:
    """Render at 200 DPI, degrade, re-wrap as an IMAGE-ONLY PDF: no text layer."""
    img = degrade(_raster(doc), "medium", _rng(doc))
    jpeg = io.BytesIO()
    img.save(jpeg, "JPEG", quality=82)
    jpeg.seek(0)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle("Scanned document")
    c.drawImage(ImageReader(jpeg), 0, 0, width=W, height=H)
    c.showPage()
    c.save()
    return buf.getvalue()


def as_png(doc: InvoiceDoc) -> bytes:
    """Single page, 200 DPI, lightly degraded."""
    buf = io.BytesIO()
    degrade(_raster(doc), "light", _rng(doc)).save(buf, "PNG", dpi=(SCAN_DPI, SCAN_DPI))
    return buf.getvalue()


def as_tiff(doc: InvoiceDoc) -> bytes:
    """1-bit fax: 200 DPI, speckle noise, hard threshold, CCITT G4."""
    rng = _rng(doc)
    a = np.asarray(_raster(doc).convert("L"), dtype=np.float32)
    speckle = rng.random(a.shape)
    a[speckle < 0.0015] = 0
    a[speckle > 0.9985] = 255
    bw = Image.fromarray(np.where(a < 165, 0, 255).astype(np.uint8), "L").convert("1", dither=Image.Dither.NONE)
    buf = io.BytesIO()
    bw.save(buf, "TIFF", compression="group4", dpi=(SCAN_DPI, SCAN_DPI))
    return buf.getvalue()


def _obscure(img: np.ndarray, rect: tuple[int, int, int, int], rng: np.random.Generator) -> np.ndarray:
    """A coffee ring and a hard shadow over one region: the text there is genuinely lost."""
    x0, y0, x1, y1 = rect
    h, w = img.shape[:2]
    pad_x, pad_y = int((x1 - x0) * 0.25), int((y1 - y0) * 1.2)
    rx0, ry0, rx1, ry1 = max(0, x0 - pad_x), max(0, y0 - pad_y), min(w, x1 + pad_x), min(h, y1 + pad_y)
    region = img[ry0:ry1, rx0:rx1].astype(np.float32)
    region = cv2.GaussianBlur(region, (0, 0), 7)
    img[ry0:ry1, rx0:rx1] = region.astype(np.uint8)

    cx, cy = (x0 + x1) / 2 + (x1 - x0) * 0.1, (y0 + y1) / 2
    rad = max(x1 - x0, (y1 - y0) * 4) * 0.95
    ring_cx, ring_cy, ring_r = cx + rad * 0.55, cy - rad * 0.15, rad * 0.75
    # Work in a window around the effects only (outside it both are zero).
    wx0 = int(max(0, min(cx - rad, ring_cx - ring_r - 20)))
    wx1 = int(min(w, max(cx + rad, ring_cx + ring_r + 20)))
    wy0 = int(max(0, min(cy - rad * 0.55, ring_cy - ring_r - 20)))
    wy1 = int(min(h, max(cy + rad * 0.55, ring_cy + ring_r + 20)))
    yy, xx = np.mgrid[wy0:wy1, wx0:wx1].astype(np.float32)
    win = img[wy0:wy1, wx0:wx1].astype(np.float32)
    # Shadow: a dark soft blob over the field
    d = np.sqrt(((xx - cx) / rad) ** 2 + ((yy - cy) / (rad * 0.55)) ** 2)
    shade = (np.clip(1.0 - d, 0, 1) ** 0.6 * 0.88)[..., None]
    win = win * (1 - shade) + np.array([62, 48, 36], dtype=np.float32) * shade
    # Coffee ring: a brown annulus, uneven, partly over the field
    dist = np.sqrt((xx - ring_cx) ** 2 + (yy - ring_cy) ** 2)
    edge = np.exp(-((dist - ring_r) / 5.0) ** 2) * (0.35 + 0.25 * np.cos(np.arctan2(yy - ring_cy, xx - ring_cx) * 3))
    alpha = np.clip(edge + (dist < ring_r).astype(np.float32) * 0.12, 0, 0.7)[..., None]
    win = win * (1 - alpha) + np.array([128, 84, 44], dtype=np.float32) * alpha
    img[wy0:wy1, wx0:wx1] = np.clip(win, 0, 255).astype(np.uint8)
    return img


def as_jpeg_photo(doc: InvoiceDoc, obscure: str | None = None, dpi: int = PHOTO_DPI) -> bytes:
    """A phone photo of the printed page: perspective warp (corners jittered up
    to 3% of page width), -1.8 deg rotation, radial shadow, gaussian blur 0.8,
    noise sigma 4, off-white paper on a grey desk, JPEG quality 72. `obscure`
    names printed text to bury under a shadow and a coffee ring."""
    rng = _rng(doc)
    page = np.asarray(_raster(doc, dpi).convert("RGB")).copy()
    if obscure:
        page = _obscure(page, _text_rect_px(doc, obscure, dpi), rng)
    page = (page.astype(np.float32) * np.array([0.985, 0.965, 0.915], dtype=np.float32)).astype(np.uint8)

    ph, pw = page.shape[:2]
    dw, dh = int(pw * 1.16), int(ph * 1.12)
    desk = np.full((dh, dw, 3), (112, 108, 101), dtype=np.float32)
    desk += rng.normal(0, 6, (dh, dw, 1))
    ox, oy = (dw - pw) / 2, (dh - ph) / 2
    src = np.float32([[0, 0], [pw, 0], [pw, ph], [0, ph]])
    jitter = rng.uniform(-0.03, 0.03, (4, 2)) * pw
    dst = np.float32(src + [ox, oy] + jitter)
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(page, m, (dw, dh), flags=cv2.INTER_CUBIC)
    mask = cv2.warpPerspective(np.ones((ph, pw), np.float32), m, (dw, dh))[..., None]
    img = desk * (1 - mask) + warped.astype(np.float32) * mask

    r = cv2.getRotationMatrix2D((dw / 2, dh / 2), -1.8, 1.0)
    img = cv2.warpAffine(img, r, (dw, dh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)

    # Radial shadow: the phone and the photographer's hand, from the lower left
    yy, xx = np.mgrid[0:dh, 0:dw].astype(np.float32)
    d = np.sqrt(((xx - dw * -0.05) / dw) ** 2 + ((yy - dh * 1.05) / dh) ** 2)
    img *= (0.58 + 0.42 * np.clip(d / 1.1, 0, 1))[..., None]

    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    img += rng.normal(0, 4, img.shape)
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=72)
    return buf.getvalue()


# ---------------------------------------------------------------- the samples

NORDIC = VendorStyle(
    name="Nordic Components Pvt Ltd",
    address=["No. 42, 2nd Main Road, Peenya Industrial Area Phase II", "Bengaluru 560058, Karnataka, India"],
    gstin="29AABCN4471K1Z3", pan="AABCN4471K", phone="+91 80 4127 6650",
    email="accounts@nordiccomponents.in", website="www.nordiccomponents.in",
    color="#0f4c5c", monogram="NC", state="Bengaluru")

NORDIC_BANK = {"Bank": "HDFC Bank Ltd, Peenya Branch", "A/c Name": "Nordic Components Pvt Ltd",
               "A/c No.": "50200012344471", "IFSC": "HDFC0000523"}


SHREEJI = VendorStyle(
    name="Shreeji Industrial Supplies",
    address=["Plot 118, Phase II, GIDC Naroda", "Ahmedabad 382330, Gujarat, India"],
    gstin="24AAKFS8820M1Z9", pan="AAKFS8820M", phone="+91 79 2282 4410",
    email="billing@shreejiindustrial.in", website="www.shreejiindustrial.in",
    color="#8a3b12", monogram="SI", state="Ahmedabad", layout="two_column")
SHREEJI_BANK = {"Bank": "ICICI Bank Ltd, Naroda GIDC Branch", "A/c Name": "Shreeji Industrial Supplies",
                "A/c No.": "184605008820", "IFSC": "ICIC0001846"}

MERIDIAN = VendorStyle(
    name="Meridian Packaging LLP",
    address=["Gala 7-9, Shree Sai Industrial Estate, Vadpe", "Bhiwandi 421302, Maharashtra, India"],
    gstin="27AASFM1109P1Z4", pan="AASFM1109P", phone="+91 22 2560 7718",
    email="accounts@meridianpack.in", website="www.meridianpack.in",
    color="#2f5d3a", monogram="MP", state="Thane")
MERIDIAN_BANK = {"Bank": "Axis Bank Ltd, Bhiwandi Branch", "A/c Name": "Meridian Packaging LLP",
                 "A/c No.": "917020045501109", "IFSC": "UTIB0000247"}

KAVERI = VendorStyle(
    name="Kaveri Logistics Pvt Ltd",
    address=["SP-14, SIDCO Industrial Estate, Guindy", "Chennai 600032, Tamil Nadu, India"],
    gstin="33AADCK6652Q1Z7", pan="AADCK6652Q", phone="+91 44 4851 2290",
    email="accounts@kaveri-logistics.co", website="www.kaveri-logistics.co",
    color="#1e3a5f", monogram="KL", state="Chennai", layout="two_column")
# Not the bank in the vendor master (SBI ...6652): Edge 4.
KAVERI_NEW_BANK = {"Bank": "Kotak Mahindra Bank, Guindy Branch", "A/c Name": "Kaveri Logistics Pvt Ltd",
                   "A/c No.": "4712009043", "IFSC": "KKBK0008817"}

ORBIT = VendorStyle(
    name="Orbit Electricals",
    address=["B-61, Okhla Industrial Area Phase I", "New Delhi 110020, Delhi, India"],
    gstin="07AAGFO3317H1Z6", pan="AAGFO3317H", phone="+91 11 4105 3317",
    email="accounts@orbitelectricals.in", website="www.orbitelectricals.in",
    color="#b45309", monogram="OE", state="Delhi")
ORBIT_BANK = {"Bank": "Kotak Mahindra Bank, Okhla Branch", "A/c Name": "Orbit Electricals",
              "A/c No.": "9811043317", "IFSC": "KKBK0000172"}

APEX = VendorStyle(
    name="Apex Tooling GmbH",
    address=["Industriestrasse 14", "70565 Stuttgart, Germany"],
    gstin="DE298765431", pan="", phone="+49 711 7820 300",
    email="invoices@apex-tooling.de", website="www.apex-tooling.de",
    color="#7c1d1d", monogram="AT", state="Stuttgart", layout="european")
APEX_BANK = {"Bank": "Commerzbank Stuttgart", "IBAN": "DE89 6004 0071 0512 3430 00", "BIC": "COBADEFFXXX"}

KA = "Karnataka (29)"


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


def ec1_meridian_photo() -> InvoiceDoc:
    """Edge 1: a phone photo; the invoice number is lost under a shadow and a coffee ring.
    PO-2043 is quoted only in the notes (Meridian's layout)."""
    return InvoiceDoc(
        filename="EC1_meridian_photo.jpg", vendor=MERIDIAN,
        invoice_no="MPL/26-27/0457", invoice_date="10/11/2026", due_date="10/12/2026", po_ref=None,
        place_of_supply=KA,
        lines=[Line("CTN-5PLY", "5-ply corrugated carton 450x300x300", "4819", Decimal(300), "NOS",
                    Decimal("50.00"), Decimal(18)),
               Line("TAPE-48", "BOPP packing tape 48mm x 65m", "3919", Decimal(120), "ROLL", Decimal("25.00"),
                    Decimal(18))],
        tax_mode="inter", freight=Decimal("660.00"), bank=MERIDIAN_BANK,
        terms="Payment: 30 days from date of invoice",
        notes=["Supplied against your Purchase Order PO-2043", "Delivered to Plant 2 stores, challan 2211"])


def ec2_nordic_third_tranche() -> InvoiceDoc:
    """Edge 2: third invoice on PO-2041; alone it fits, cumulatively it is Rs 42,000 over."""
    return InvoiceDoc(
        filename="EC2_nordic_third_tranche.pdf", vendor=NORDIC,
        invoice_no="INV-NC-5547", invoice_date="12/11/2026", due_date="12/12/2026", po_ref="PO-2041",
        place_of_supply=KA,
        lines=[Line("BRG-12", "Deep groove ball bearing 6204-2RS", "8482", Decimal(120), "NOS", Decimal("750.00"),
                    Decimal(18)),
               Line("SHF-20", "Precision ground shaft 20mm x 300mm", "8483", Decimal(40), "NOS",
                    Decimal("1500.00"), Decimal(18)),
               Line("SEAL-8", "Nitrile oil seal 35x52x8", "4016", Decimal(240), "NOS", Decimal("50.00"),
                    Decimal(18))],
        tax_mode="intra", freight=Decimal("840.00"), bank=NORDIC_BANK,
        terms="Payment: Net 30 days from invoice date",
        notes=["Third tranche against PO-2041", "Goods dispatched via Safexpress, LR 88214471"])


def ec3_shreeji_reissue() -> InvoiceDoc:
    """Edge 3: the same goods as INV-SH-7781 (Rs 46,200, paid), re-issued two days later under a new number."""
    return InvoiceDoc(
        filename="EC3_shreeji_reissue.pdf", vendor=SHREEJI,
        invoice_no="INV-SH-7793", invoice_date="04/11/2026", due_date="19/12/2026", po_ref="PO-2052",
        place_of_supply=KA,
        lines=[Line("WLD-ROD", "Welding electrode E6013 3.15mm", "8311", Decimal(25), "BOX",
                    Decimal("1200.00"), Decimal(18)),
               Line("GRD-DSC", "Grinding disc 100x6x16mm", "6804", Decimal(75), "NOS", Decimal("120.00"),
                    Decimal(18))],
        tax_mode="inter", freight=Decimal("180.00"), bank=SHREEJI_BANK,
        terms="Payment: 45 days from invoice date",
        notes=["Transport: Shree Maruti Courier, docket 55102874"])


def ec4_kaveri_redirect() -> InvoiceDoc:
    """Edge 4: a perfect match on every rule, paid into a different bank account."""
    return InvoiceDoc(
        filename="EC4_kaveri_redirect.pdf", vendor=KAVERI,
        invoice_no="KLPL/2026/1241", invoice_date="08/11/2026", due_date="23/11/2026", po_ref="PO-2044",
        place_of_supply=KA,
        lines=[Line("WH-PLT", "Pallet storage, October 2026", "996729", Decimal(700), "PLT",
                    Decimal("220.00"), Decimal(18)),
               Line("HDL-IN", "Inbound handling and put-away", "996719", Decimal(1000), "PKG", Decimal("45.00"),
                    Decimal(18)),
               Line("HDL-OUT", "Outbound picking and dispatch", "996719", Decimal(1000), "PKG", Decimal("45.00"),
                    Decimal(18))],
        tax_mode="inter", freight=Decimal("10480.00"), bank=KAVERI_NEW_BANK,
        terms="Payment: 15 days from invoice date",
        notes=["Please note our updated bank details for all future payments",
               "Line-haul Chennai - Bengaluru included under freight"])


def ec5_orbit_variance() -> InvoiceDoc:
    """Optional 5: 2.4% over a PO that does not allow partials, and only 80 of 100 boards received."""
    return InvoiceDoc(
        filename="EC5_orbit_variance.pdf", vendor=ORBIT,
        invoice_no="OE/2026/583", invoice_date="09/11/2026", due_date="09/12/2026", po_ref="PO-2047",
        place_of_supply=KA,
        lines=[Line("DB-8W", "8-way TPN distribution board, double door", "8537", Decimal(100), "NOS",
                    Decimal("1700.00"), Decimal(18))],
        tax_mode="inter", freight=Decimal("4200.00"), bank=ORBIT_BANK,
        terms="Payment: Net 30 days",
        notes=["Prices revised per our letter OE/PR/26/14"])


def ec6_apex_eur() -> InvoiceDoc:
    """Optional 6: a EUR invoice quoting PO-2045, a closed INR PO that belongs to Nordic."""
    return InvoiceDoc(
        filename="EC6_apex_eur.pdf", vendor=APEX,
        invoice_no="AT-2026-0912", invoice_date="06.11.2026", due_date="05.01.2027", po_ref="PO-2045",
        place_of_supply="India",
        lines=[Line("TL-EM10", "Solid carbide end mill D10 Z4", "8207.70", Decimal(40), "pc", Decimal("95.00"),
                    Decimal(0)),
               Line("TL-HSK63", "HSK-A63 shrink-fit chuck D12", "8466.10", Decimal(6), "pc", Decimal("530.00"),
                    Decimal(0))],
        tax_mode="export", freight=Decimal("0.00"), bank=APEX_BANK, currency_label="EUR",
        terms="Payment terms: 60 days net from invoice date.",
        notes=["Delivery terms: FCA Stuttgart (Incoterms 2020).",
               "Tax-free export delivery; the recipient accounts for import tax."])


def fmt_orbit_scan() -> InvoiceDoc:
    """Format check: an image-only scanned PDF of a clean invoice (PO-2051 has Rs 65,650 left)."""
    return InvoiceDoc(
        filename="FMT_orbit_scan.pdf", vendor=ORBIT,
        invoice_no="OE/2026/598", invoice_date="11/11/2026", due_date="11/12/2026", po_ref="PO-2051",
        place_of_supply=KA,
        lines=[Line("MCB-32C", "MCB 32A C-curve, 10kA, 1 pole", "8536", Decimal(100), "NOS", Decimal("255.00"),
                    Decimal(18))],
        tax_mode="inter", freight=Decimal("0.00"), bank=ORBIT_BANK,
        terms="Payment: Net 30 days", notes=["Part delivery against PO-2051"])


def fmt_meridian_png() -> InvoiceDoc:
    """Format check: a PNG scan (PO-2049 has Rs 20,720 left; PO quoted in the notes)."""
    return InvoiceDoc(
        filename="FMT_meridian_scan.png", vendor=MERIDIAN,
        invoice_no="MPL/26-27/0466", invoice_date="13/11/2026", due_date="13/12/2026", po_ref=None,
        place_of_supply=KA,
        lines=[Line("STR-23", "Stretch film 23 micron x 500mm", "3920", Decimal(50), "ROLL", Decimal("280.00"),
                    Decimal(18))],
        tax_mode="inter", freight=Decimal("0.00"), bank=MERIDIAN_BANK,
        terms="Payment: 30 days from date of invoice",
        notes=["Supplied against your Purchase Order PO-2049"])


def fmt_shreeji_tiff() -> InvoiceDoc:
    """Format check: a 1-bit fax TIFF (PO-2052 has Rs 98,800 left)."""
    return InvoiceDoc(
        filename="FMT_shreeji_fax.tif", vendor=SHREEJI,
        invoice_no="INV-SH-7802", invoice_date="12/11/2026", due_date="27/12/2026", po_ref="PO-2052",
        place_of_supply=KA,
        lines=[Line("GRD-DSC", "Grinding disc 100x6x16mm", "6804", Decimal(50), "NOS", Decimal("120.00"),
                    Decimal(18))],
        tax_mode="inter", freight=Decimal("0.00"), bank=SHREEJI_BANK,
        terms="Payment: 45 days from invoice date", notes=[])


SAMPLES: list[tuple[Callable[[], InvoiceDoc], Callable[[InvoiceDoc], bytes]]] = [
    (happy_path, as_clean_pdf),
    (ec1_meridian_photo, lambda d: as_jpeg_photo(d, obscure=d.invoice_no)),
    (ec2_nordic_third_tranche, as_clean_pdf),
    (ec3_shreeji_reissue, as_clean_pdf),
    (ec4_kaveri_redirect, as_clean_pdf),
    (ec5_orbit_variance, as_clean_pdf),
    (ec6_apex_eur, as_clean_pdf),
    (fmt_orbit_scan, as_scanned_pdf),
    (fmt_meridian_png, as_png),
    (fmt_shreeji_tiff, as_tiff),
]


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
