"""S05 Normalise: the model's verbatim strings -> typed values. Pure Python, no LLM.

Every conversion that could be a guess is either resolved from master data
(date order from the vendor's locale) or flagged as a warning with all readings
kept. Human-supplied values (Edge 1's "supply missing field") enter here.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app import config
from app.models import InvoiceLine, NormalisedInvoice, PoRef, RunContext, StageResult, TaxComponent, to_jsonable
from app.services import llm
from app.services.matching import canonical_vendor_name, resolve_vendor
from app.services.money import detect_currency, format_money, parse_amount

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# PO-like tokens: "PO-2041", "P.O. 2041", "PO No: 2041", "PO#2041",
# "Purchase Order No. 2041", "against order 2041".
_PO_PATTERNS = [
    re.compile(r"\bP\.?\s?O\.?\s*(?:No\.?|Number|Ref\.?|#)?\s*[:#-]?\s*(\d{3,6})\b", re.IGNORECASE),
    re.compile(r"\border\s*(?:No\.?|Number|Ref\.?|#)?\s*[:#-]?\s*(?:PO[\s-]?)?(\d{4})\b", re.IGNORECASE),
]


# ---------------------------------------------------------------- field access

def _get(payload: dict[str, Any], path: str) -> dict[str, Any] | None:
    node: Any = payload
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            node = node[idx] if isinstance(node, list) and idx < len(node) else None
        else:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def _value(payload: dict[str, Any], path: str) -> str | None:
    f = _get(payload, path)
    v = f.get("value") if f else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def apply_overrides(payload: dict[str, Any], overrides: dict[str, str]) -> None:
    """Write clerk-supplied values into the payload as confidence-1.0 readings."""
    for path, value in overrides.items():
        f = _get(payload, path)
        if f is None:
            raise ValueError(f"cannot supply {path!r}: no such field in the extraction contract")
        f.update({"value": value, "confidence": 1.0, "evidence": None, "page": None})


# ---------------------------------------------------------------- dates

def parse_date(text: str | None, order: str | None) -> tuple[date | None, list[date], str | None]:
    """Return (chosen date, all plausible readings, warning).

    order is "DMY" or "MDY" from the vendor's locale, or None if unknown.
    """
    if not text:
        return None, [], None
    s = text.strip().lower().replace(",", " ")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)

    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        d = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        return d, [d] if d else [], None if d else f"invalid date {text!r}"

    m = re.fullmatch(r"(\d{1,2})[-/. ]+([a-z]{3,9})[-/. ]+(\d{2,4})", s)          # 15 Mar 2026, 15-MAR-26
    if m and m[2][:3] in _MONTHS:
        d = _safe_date(_year(m[3]), _MONTHS[m[2][:3]], int(m[1]))
        return d, [d] if d else [], None if d else f"invalid date {text!r}"
    m = re.fullmatch(r"([a-z]{3,9})\s+(\d{1,2})\s+(\d{2,4})", s)                   # March 15 2026
    if m and m[1][:3] in _MONTHS:
        d = _safe_date(_year(m[3]), _MONTHS[m[1][:3]], int(m[2]))
        return d, [d] if d else [], None if d else f"invalid date {text!r}"

    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if not m:
        return None, [], f"unrecognised date format {text!r}"
    a, b, y = int(m[1]), int(m[2]), _year(m[3])
    dmy, mdy = _safe_date(y, b, a), _safe_date(y, a, b)
    readings = [d for d in dict.fromkeys([dmy, mdy]) if d]
    if not readings:
        return None, [], f"invalid date {text!r}"
    if len(readings) == 1:
        return readings[0], readings, None          # only one reading is a real date
    if order in ("DMY", "MDY"):
        return (dmy if order == "DMY" else mdy), readings, None
    return dmy, readings, (f"date {text!r} is ambiguous ({dmy.isoformat()} or {mdy.isoformat()}) "
                           "and the vendor's locale is unknown")


def _year(y: str) -> int:
    return int(y) + 2000 if len(y) == 2 else int(y)


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


# ---------------------------------------------------------------- numbers

def _amount(payload: dict[str, Any], path: str, warnings: list[str]) -> int | None:
    v = _value(payload, path)
    if v is None:
        return None
    try:
        return parse_amount(v)
    except ValueError:
        warnings.append(f"could not read an amount from {path} = {v!r}")
        return None


def _decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    try:
        return Decimal(cleaned) if cleaned not in ("", "-", ".") else None
    except InvalidOperation:
        return None


def _rate(text: str | None) -> Decimal | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?", text)
    return Decimal(m[1]) if m else None


# ---------------------------------------------------------------- PO references

def find_po_refs(text: str, source: str, explicit: bool) -> list[PoRef]:
    refs = []
    for pat in _PO_PATTERNS:
        for m in pat.finditer(text or ""):
            refs.append(PoRef(value=f"PO-{m[1]}", raw=m[0].strip(), explicit=explicit, source=source))
    return refs


def _collect_po_refs(ctx: RunContext, payload: dict[str, Any]) -> list[PoRef]:
    refs: list[PoRef] = []
    for r in payload.get("po_references", []):
        found = find_po_refs(r["value"], "extraction", r["explicit"])
        if not found and re.fullmatch(r"\d{3,6}", r["value"].strip()):
            found = [PoRef(f"PO-{r['value'].strip()}", r["value"], r["explicit"], "extraction")]
        refs.extend(found)
    # Implied references buried in other fields (notes, line descriptions)…
    for path, f in llm.iter_fields(payload):
        for text in (f.get("value"), f.get("evidence")):
            if isinstance(text, str):
                refs.extend(find_po_refs(text, f"field:{path}", explicit=False))
    refs.extend(find_po_refs(payload.get("notes", ""), "field:notes", explicit=False))
    # …and in the document's own text, independent of what the model chose to report.
    for i, page_text in enumerate([*ctx.text_by_page, *ctx.ocr_text_by_page]):
        refs.extend(find_po_refs(page_text, "document_text", explicit=False))
    seen: dict[str, PoRef] = {}
    for r in refs:                       # explicit readings win over implied ones
        if r.value not in seen or (r.explicit and not seen[r.value].explicit):
            seen[r.value] = r
    return sorted(seen.values(), key=lambda r: (not r.explicit, r.source != "extraction"))


# ---------------------------------------------------------------- the stage

def normalise(ctx: RunContext, payload: dict[str, Any]) -> NormalisedInvoice:
    warnings: list[str] = []
    supplier_name = _value(payload, "supplier.name")
    supplier_tax = _value(payload, "supplier.tax_id")

    vendor = None
    if ctx.ref:
        vm = resolve_vendor(supplier_name, supplier_tax, ctx.ref.vendors)
        vendor = ctx.ref.vendors.get(vm.vendor_id) if vm.vendor_id else None

    # Currency: symbol, then ISO code, then vendor default — in that order.
    texts = [_value(payload, p) for p in ("currency", "totals.grand_total", "totals.subtotal")]
    found = [detect_currency(t) for t in texts]
    currency, cur_source = next(((c, s) for c, s in found if s == "symbol"),
                                next(((c, s) for c, s in found if s == "iso_code"), (None, None)))
    if currency is None and vendor:
        currency, cur_source = vendor.default_currency, "vendor_default"

    order = config.DATE_ORDER_BY_CURRENCY.get(vendor.default_currency if vendor else (currency or ""))
    inv_date, readings, date_warning = parse_date(_value(payload, "invoice_date"), order)
    if date_warning:
        warnings.append(date_warning)
    # Keep both readings only when the ambiguity is unresolved; R09 routes it to a human.
    alternatives = readings if date_warning and len(readings) > 1 else []
    due_date, _, due_warning = parse_date(_value(payload, "due_date"), order)
    if due_warning:
        warnings.append(f"due_date: {due_warning}")

    lines = []
    for i, li in enumerate(payload.get("line_items", [])):
        p = f"line_items[{i}]"
        lines.append(InvoiceLine(
            line_no=li.get("line_no", i + 1),
            description=_value(payload, f"{p}.description"),
            sku=_value(payload, f"{p}.sku"),
            qty=_decimal(_value(payload, f"{p}.qty")),
            uom=_value(payload, f"{p}.uom"),
            unit_price_minor=_amount(payload, f"{p}.unit_price", warnings),
            tax_rate_pct=_rate(_value(payload, f"{p}.tax_rate_pct")),
            line_total_minor=_amount(payload, f"{p}.line_total", warnings),
        ))

    breakdown = []
    for i, t in enumerate(payload["totals"].get("tax_breakdown", [])):
        breakdown.append(TaxComponent(label=t["label"],
                                      amount_minor=_amount(payload, f"totals.tax_breakdown[{i}].amount", warnings),
                                      rate_pct=_rate(t["label"]) if "%" in t["label"] else None))

    subtotal = _amount(payload, "totals.subtotal", warnings)
    discount = _amount(payload, "totals.discount", warnings)
    tax_total = _amount(payload, "totals.tax_total", warnings)
    taxable =(subtotal - (discount or 0)) if subtotal is not None else None
    tax_for_rate = tax_total if tax_total is not None else (
        sum(b.amount_minor for b in breakdown) if breakdown and all(b.amount_minor is not None for b in breakdown)
        else None)
    effective = (Decimal(tax_for_rate * 100) / Decimal(taxable)).quantize(Decimal("0.01")) \
        if tax_for_rate is not None and taxable else None

    acct = _value(payload, "payment.account_number")
    last4 = "".join(c for c in acct if c.isalnum())[-4:] if acct else None
    ifsc = _value(payload, "payment.ifsc_or_swift")

    return NormalisedInvoice(
        document_type=payload["document_type"],
        invoice_number=_value(payload, "invoice_number"),
        invoice_date=inv_date, invoice_date_alternatives=alternatives,
        due_date=due_date,
        currency=currency, currency_source=cur_source,
        supplier_name=supplier_name, supplier_name_canonical=canonical_vendor_name(supplier_name),
        supplier_tax_id=supplier_tax, supplier_email=_value(payload, "supplier.email"),
        buyer_name=_value(payload, "buyer.name"), buyer_tax_id=_value(payload, "buyer.tax_id"),
        po_refs=_collect_po_refs(ctx, payload),
        lines=lines,
        subtotal_minor=subtotal, discount_minor=discount, tax_total_minor=tax_total,
        tax_breakdown=breakdown, shipping_minor=_amount(payload, "totals.shipping", warnings),
        grand_total_minor=_amount(payload, "totals.grand_total", warnings),
        effective_tax_rate_pct=effective,
        bank_account_last4=last4,
        bank_ifsc=re.sub(r"\s+", "", ifsc).upper() if ifsc else None,
        beneficiary_name=_value(payload, "payment.beneficiary_name"),
        confidence={path: float(f["confidence"]) for path, f in llm.iter_fields(payload)},
        human_supplied=sorted(ctx.overrides),
        warnings=warnings,
    )


def run(ctx: RunContext) -> StageResult:
    if ctx.extraction is None:
        return StageResult("failed", "no extraction to normalise")
    # Work on a copy: the stored model output must stay exactly as the model returned it.
    payload = copy.deepcopy(ctx.extraction)
    if ctx.overrides:
        apply_overrides(payload, ctx.overrides)
    ctx.extraction = payload
    inv = normalise(ctx, payload)
    ctx.invoice = inv
    detail = to_jsonable(inv)
    bits = [format_money(inv.grand_total_minor, inv.currency or "INR")
            if inv.grand_total_minor is not None else "no grand total",
            f"dated {inv.invoice_date.isoformat()}" if inv.invoice_date else "no date",
            f"{len(inv.lines)} line{'s' if len(inv.lines) != 1 else ''}",
            f"PO refs: {', '.join(r.value for r in inv.po_refs) or 'none'}"]
    if inv.human_supplied:
        bits.append(f"clerk supplied {', '.join(inv.human_supplied)}")
    status = "warn" if inv.warnings else "ok"
    summary = "; ".join(bits) + (f" — {len(inv.warnings)} warning(s)" if inv.warnings else "")
    return StageResult(status, summary, detail)
