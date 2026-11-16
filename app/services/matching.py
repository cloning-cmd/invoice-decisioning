"""Fuzzy vendor resolution, PO candidate scoring, and evidence-to-bbox location.

Pure functions over reference data: no database, no network.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from rapidfuzz import fuzz

from app import config
from app.models import InvoiceLine, POMatch, PoRef, PurchaseOrder, Vendor, VendorMatch


# ---------------------------------------------------------------- vendor names

def canonical_vendor_name(name: str | None) -> str | None:
    """'Nordic Comp. Pvt. Ltd.' -> 'NORDIC COMP'. Uppercase, no punctuation, no legal suffix."""
    if not name:
        return None
    s = name.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    tokens = s.split()
    # Strip legal suffixes from the end, repeatedly ("PRIVATE LIMITED", then "CO").
    suffixes = sorted((x.split() for x in config.VENDOR_LEGAL_SUFFIXES), key=len, reverse=True)
    changed = True
    while changed and tokens:
        changed = False
        for suf in suffixes:
            if len(tokens) > len(suf) and tokens[-len(suf):] == suf:
                tokens = tokens[:-len(suf)]
                changed = True
                break
    return " ".join(tokens) or None


def _norm_tax_id(tax_id: str | None) -> str | None:
    return re.sub(r"[^A-Z0-9]", "", tax_id.upper()) if tax_id else None


def resolve_vendor(name: str | None, tax_id: str | None, vendors: dict[str, Vendor]) -> VendorMatch:
    """Exact canonical name, then alias, then tax id, then fuzzy — first hit wins."""
    canon = canonical_vendor_name(name)
    if canon:
        for v in vendors.values():
            if canonical_vendor_name(v.legal_name) == canon:
                return VendorMatch(v.id, "exact", 100.0, False, [])
        for v in vendors.values():
            if any(canonical_vendor_name(a) == canon for a in v.aliases):
                return VendorMatch(v.id, "alias", 100.0, False, [])
    tid = _norm_tax_id(tax_id)
    if tid:
        for v in vendors.values():
            if _norm_tax_id(v.tax_id) == tid:
                return VendorMatch(v.id, "tax_id", 100.0, False, [])
    if not canon:
        return VendorMatch(None, "none", 0.0, False, [])

    scored = []
    for v in vendors.values():
        names = [v.legal_name, *v.aliases]
        best = max(fuzz.token_set_ratio(canon, canonical_vendor_name(n) or "") for n in names)
        scored.append({"vendor_id": v.id, "legal_name": v.legal_name, "score": round(best, 1)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    top = scored[: config.PO_CANDIDATES_SHOWN]
    best = scored[0]
    if best["score"] >= config.VENDOR_MATCH_MIN:
        return VendorMatch(best["vendor_id"], "fuzzy", best["score"], False, top)
    if best["score"] >= config.VENDOR_MATCH_AMBIGUOUS_MIN:
        return VendorMatch(best["vendor_id"], "fuzzy", best["score"], True, top)
    return VendorMatch(None, "none", best["score"], False, top)


# ---------------------------------------------------------------- PO matching

def _amount_score(invoice_total: int | None, remaining: int) -> float:
    if invoice_total is None or remaining <= 0:
        return 0.0
    gap = abs(invoice_total - remaining) / remaining
    return max(0.0, 1.0 - gap / config.PO_AMOUNT_SCORE_ZERO_PCT)


def _date_score(invoice_date: date | None, po: PurchaseOrder) -> float:
    if invoice_date is None:
        return 0.0  # no evidence either way; do not reward a missing date
    if invoice_date < po.issue_date:
        outside = (po.issue_date - invoice_date).days
    elif po.expiry_date and invoice_date > po.expiry_date:
        outside = (invoice_date - po.expiry_date).days
    else:
        return 1.0
    return max(0.0, 1.0 - outside / config.PO_DATE_SCORE_DECAY_DAYS)


def line_to_po_line(line: InvoiceLine, po: PurchaseOrder) -> tuple[str | None, float]:
    """Best PO line SKU for an invoice line: exact SKU, else fuzzy description."""
    if line.sku:
        for pl in po.lines:
            if pl.sku.upper() == line.sku.strip().upper():
                return pl.sku, 100.0
    if line.description:
        best = max(((pl.sku, fuzz.token_set_ratio(line.description.upper(), pl.description.upper()))
                    for pl in po.lines), key=lambda t: t[1], default=(None, 0.0))
        if best[1] >= config.LINE_DESC_MATCH_MIN:
            return best
        return None, best[1]
    return None, 0.0


def _lines_score(lines: list[InvoiceLine], po: PurchaseOrder) -> float:
    if not lines or not po.lines:
        return 0.0
    return sum(line_to_po_line(l, po)[1] for l in lines) / (100.0 * len(lines))


def score_po_candidate(po: PurchaseOrder, remaining: int, invoice_total: int | None,
                       invoice_date: date | None, lines: list[InvoiceLine]) -> dict[str, Any]:
    a = _amount_score(invoice_total, remaining)
    d = _date_score(invoice_date, po)
    l = _lines_score(lines, po)
    total = (config.PO_SCORE_WEIGHT_AMOUNT * a + config.PO_SCORE_WEIGHT_DATE * d
             + config.PO_SCORE_WEIGHT_LINES * l)
    return {"po_number": po.po_number, "score": round(total, 3), "amount": round(a, 3),
            "date": round(d, 3), "lines": round(l, 3), "remaining_minor": remaining,
            "po_total_minor": po.po_total_minor, "status": po.status}


def match_po(refs: list[PoRef], vendor_id: str | None, pos: dict[str, PurchaseOrder],
             remaining_by_po: dict[str, int], invoice_total: int | None,
             invoice_date: date | None, lines: list[InvoiceLine]) -> POMatch:
    """Tiered PO match. T1 explicit+own vendor, T2 explicit+other vendor (hard stop),
    T3 scored search over the vendor's open POs."""
    quoted = [r.value for r in refs]
    unknown = [q for q in quoted if q not in pos]

    def scored(include: str | None = None) -> list[dict[str, Any]]:
        cands = [score_po_candidate(po, remaining_by_po.get(po.po_number, po.po_total_minor),
                                    invoice_total, invoice_date, lines)
                 for po in pos.values()
                 if po.vendor_id == vendor_id and (po.status in ("open", "partially_invoiced")
                                                   or po.po_number == include)]
        return sorted(cands, key=lambda c: c["score"], reverse=True)

    for q in quoted:
        po = pos.get(q)
        if po and vendor_id and po.vendor_id == vendor_id:
            # The explicit reference decides; the scores are shown only as
            # corroboration (does the quoted PO also look like the best fit?).
            return POMatch("T1", q, q, scored(include=q)[: config.PO_CANDIDATES_SHOWN],
                           f"{q} is quoted on the invoice and belongs to {vendor_id}")
    for q in quoted:
        po = pos.get(q)
        if po:
            # Exists but belongs to someone else: a near miss here is a hard
            # failure, never a fallback to "find something similar".
            return POMatch("T2", None, q, [],
                           f"{q} is quoted but belongs to {po.vendor_id}, not {vendor_id or 'an unresolved vendor'}")

    if vendor_id is None:
        return POMatch(None, None, quoted[0] if quoted else None, [],
                       "vendor not resolved, so no PO candidates can be searched")

    cands = scored()
    prefix = f"quoted {', '.join(unknown)} not found; " if unknown else ""
    if not cands:
        return POMatch(None, None, unknown[0] if unknown else None, [],
                       prefix + f"{vendor_id} has no open POs")
    top = cands[0]
    margin = top["score"] - cands[1]["score"] if len(cands) > 1 else top["score"]
    shown = cands[: config.PO_CANDIDATES_SHOWN]
    if top["score"] >= config.PO_AUTOMATCH_MIN and margin >= config.PO_MARGIN_MIN:
        return POMatch("T3", top["po_number"], unknown[0] if unknown else None, shown,
                       prefix + f"implied match {top['po_number']} scored {top['score']:.2f}, "
                                f"margin {margin:.2f} over the runner-up")
    why = (f"top score {top['score']:.2f} below {config.PO_AUTOMATCH_MIN:.2f}"
           if top["score"] < config.PO_AUTOMATCH_MIN
           else f"margin {margin:.2f} below {config.PO_MARGIN_MIN:.2f}")
    return POMatch(None, None, unknown[0] if unknown else None, shown,
                   prefix + f"no confident implied match ({why}); a human should choose")


# ---------------------------------------------------------------- evidence -> bbox

def _norm_token(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())


def locate_evidence(evidence: str | None, page: int | None,
                    word_boxes: list[dict[str, Any]]) -> list[float] | None:
    """Find the evidence string among the page's words; return the union bbox (0-1) or None.

    Slides a window of the evidence's token count over the page's words and
    keeps the best fuzzy match. Accepts it only above EVIDENCE_MATCH_MIN, so a
    wrong box is never drawn — a missing box is the honest fallback.
    """
    if not evidence:
        return None
    target_tokens = [t for t in (_norm_token(x) for x in evidence.split()) if t]
    if not target_tokens:
        return None
    target = " ".join(target_tokens)
    pages = [page] if page else sorted({w["page"] for w in word_boxes})
    best: tuple[float, list[dict[str, Any]]] = (0.0, [])
    for p in pages:
        words = [w for w in word_boxes if w["page"] == p and _norm_token(w["text"])]
        n = len(target_tokens)
        for size in {n, max(1, n - 1), n + 1}:
            for i in range(0, max(0, len(words) - size) + 1):
                window = words[i:i + size]
                cand = " ".join(_norm_token(w["text"]) for w in window)
                score = fuzz.ratio(target, cand)
                if score > best[0]:
                    best = (score, window)
    score, window = best
    if score < config.EVIDENCE_MATCH_MIN or not window:
        return None
    xs0, ys0, xs1, ys1 = zip(*(w["bbox"] for w in window))
    return [round(min(xs0), 4), round(min(ys0), 4), round(max(xs1), 4), round(max(ys1), 4)]


def locate_field(field: dict[str, Any], word_boxes: list[dict[str, Any]]) -> list[float] | None:
    """The box for an extracted field: its evidence, else one line of it, else its value.

    Evidence often spans a label and a value (label "INVOICE NO." on one line,
    "OE/2026/598" on the next). On a digital PDF those words are adjacent; in
    OCR reading order a header strip reads all labels first, then all values,
    so the pair never appears side by side. The fallbacks find the value on its
    own. Short strings (under 4 characters, e.g. "18%") are not searched alone:
    too many places match.
    """
    page = field.get("page")
    evidence, value = field.get("evidence"), field.get("value")
    tries = [evidence]
    if evidence and "\n" in evidence:
        lines = [ln for ln in evidence.split("\n") if ln.strip()]
        tries += sorted(lines, key=len, reverse=True)
    tries.append(value)
    for text in tries:
        if text and len(text.strip()) >= 4:
            bbox = locate_evidence(text, page, word_boxes)
            if bbox:
                return bbox
    return None

