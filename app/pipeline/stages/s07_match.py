"""S07 Match: vendor, PO (tiered), cumulative PO consumption, GRN three-way, duplicates.

All lookups against history happen here, so the rules after it are pure
functions of what this stage found.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app import config, db
from app.models import (Consumption, DuplicateCheck, GrnCheck, RunContext, StageResult, to_jsonable)
from app.services.matching import line_to_po_line, match_po, resolve_vendor
from app.services.money import format_money

APPROVED_VERDICTS = ("AUTO_APPROVE", "APPROVE_PENDING_SIGNOFF")


def _consumption(conn, po_number: str, po_total: int, this_total: int | None) -> Consumption:
    placeholders = ",".join("?" * len(config.PO_CONSUMING_STATUSES))
    rows = db.query(conn, f"""SELECT invoice_number, invoice_date, total_minor, status FROM invoice_history
                              WHERE po_number = ? AND status IN ({placeholders})
                              ORDER BY invoice_date""", (po_number, *config.PO_CONSUMING_STATUSES))
    consumed = sum(r["total_minor"] for r in rows)
    return Consumption(po_number, po_total, consumed, po_total - consumed, rows, this_total)


def _grn_check(ctx: RunContext, po) -> GrnCheck:
    grns = ctx.ref.grns_by_po_id.get(po.id, ())
    received: dict[str, Decimal] = defaultdict(Decimal)
    for g in grns:
        for sku, qty in g.lines:
            received[sku] += qty
    invoiced: dict[str, Decimal] = defaultdict(Decimal)
    unmatched = []
    for line in ctx.invoice.lines:
        sku, score = line_to_po_line(line, po)
        if sku is None or line.qty is None:
            unmatched.append({"line_no": line.line_no, "description": line.description, "sku": line.sku,
                              "best_score": round(score, 1)})
            continue
        invoiced[sku] += line.qty
    lines = [{"sku": sku, "invoiced": str(qty), "received": str(received.get(sku, Decimal(0))),
              "short_by": str(max(Decimal(0), qty - received.get(sku, Decimal(0))))}
             for sku, qty in invoiced.items()]
    return GrnCheck(po.po_number, [g.grn_number for g in grns], lines, unmatched)


def _duplicates(conn, ctx: RunContext, vendor_id: str | None) -> DuplicateCheck:
    inv = ctx.invoice
    exact: list[dict[str, Any]] = []
    for r in db.query(conn, "SELECT id, invoice_number, invoice_date, total_minor, status FROM invoice_history "
                            "WHERE file_sha256 = ?", (ctx.sha256,)):
        exact.append({"kind": "same_file_in_history", **r})
    # A previous run of the same bytes only counts if it was cleared for payment;
    # re-submitting a rejected file is a correction attempt, not a duplicate.
    for r in db.query(conn, f"""
            SELECT r.id, r.created_at, r.decision FROM runs r
            WHERE r.file_sha256 = ? AND r.id != ? AND r.status = 'completed'
              AND (r.decision IN ({",".join("?" * len(APPROVED_VERDICTS))})
                   OR EXISTS (SELECT 1 FROM audit_log a WHERE a.run_id = r.id AND a.action = 'override_approve'))
            ORDER BY r.created_at""", (ctx.sha256, ctx.run_id, *APPROVED_VERDICTS)):
        exact.append({"kind": "same_file_already_approved", "run_id": r["id"], "created_at": r["created_at"],
                      "decision": r["decision"]})
    near: list[dict[str, Any]] = []
    if vendor_id and inv.invoice_number:
        for r in db.query(conn, "SELECT id, invoice_number, invoice_date, total_minor, status, po_number "
                                "FROM invoice_history WHERE vendor_id = ? AND invoice_number = ?",
                          (vendor_id, inv.invoice_number)):
            exact.append({"kind": "same_vendor_and_number", **r})
    if vendor_id and inv.grand_total_minor is not None and inv.invoice_date:
        lo = (inv.invoice_date - timedelta(days=config.DUP_DATE_WINDOW_DAYS)).isoformat()
        hi = (inv.invoice_date + timedelta(days=config.DUP_DATE_WINDOW_DAYS)).isoformat()
        for r in db.query(conn, """SELECT id, invoice_number, invoice_date, total_minor, status, po_number
                                   FROM invoice_history
                                   WHERE vendor_id = ? AND total_minor = ? AND invoice_date BETWEEN ? AND ?
                                     AND invoice_number != ?""",
                          (vendor_id, inv.grand_total_minor, lo, hi, inv.invoice_number or "")):
            near.append(r)
    return DuplicateCheck(exact, near)


def run(ctx: RunContext) -> StageResult:
    inv, ref = ctx.invoice, ctx.ref
    vm = resolve_vendor(inv.supplier_name, inv.supplier_tax_id, ref.vendors)
    ctx.vendor_match = vm

    with db.session(ctx.db_path) as conn:
        # Remaining budget per open PO of this vendor, for T3 amount scoring.
        remaining = {}
        if vm.vendor_id:
            for po in ref.pos.values():
                if po.vendor_id == vm.vendor_id:
                    remaining[po.po_number] = _consumption(conn, po.po_number, po.po_total_minor, None).remaining_minor
        pm = match_po(inv.po_refs, vm.vendor_id, ref.pos, remaining, inv.grand_total_minor,
                      inv.invoice_date, inv.lines)
        ctx.po_match = pm
        po = ref.pos.get(pm.po_number) if pm.po_number else None
        if po:
            ctx.consumption = _consumption(conn, po.po_number, po.po_total_minor, inv.grand_total_minor)
            ctx.grn = _grn_check(ctx, po)
            ctx.grn_po_has_receipts = bool(ref.grns_by_po_id.get(po.id))
        ctx.duplicates = _duplicates(conn, ctx, vm.vendor_id)

    vendor = ref.vendors.get(vm.vendor_id) if vm.vendor_id else None
    detail = {"vendor": {**to_jsonable(vm), "legal_name": vendor.legal_name if vendor else None,
                         "status": vendor.status if vendor else None},
              "po": to_jsonable(pm),
              "consumption": to_jsonable(ctx.consumption),
              "grn": to_jsonable(ctx.grn),
              "duplicates": to_jsonable(ctx.duplicates)}

    v_txt = (f"vendor {vm.vendor_id} via {vm.method}" + (f" ({vm.score:g}, ambiguous)" if vm.ambiguous else "")
             if vm.vendor_id else "vendor not found in master")
    if pm.tier in ("T1", "T3"):
        c = ctx.consumption
        cur = po.currency
        p_txt = (f"{pm.po_number} ({pm.tier}); consumed {format_money(c.consumed_minor, cur)} of "
                 f"{format_money(c.po_total_minor, cur)}, remaining {format_money(c.remaining_minor, cur)}")
        g = ctx.grn
        g_txt = f"; GRN {', '.join(g.grn_numbers)}" if g.grn_numbers else "; no GRN"
    elif pm.tier == "T2":
        p_txt, g_txt = f"{pm.quoted_po_number} belongs to another vendor (T2)", ""
    else:
        p_txt, g_txt = f"no PO matched — {pm.reason}", ""
    d = ctx.duplicates
    d_txt = (f"; {len(d.exact)} exact / {len(d.near)} near duplicate(s)" if (d.exact or d.near) else "")
    status = "ok" if vm.vendor_id and pm.tier in ("T1", "T3") and not vm.ambiguous else "warn"
    return StageResult(status, f"{v_txt}; {p_txt}{g_txt}{d_txt}", detail)
