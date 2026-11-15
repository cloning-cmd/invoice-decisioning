"""S10 Persist: the final run record, extracted fields with resolved bboxes,
the system audit entry, and the reading artefact used by replays."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from app import config, db
from app.models import NormalisedInvoice, RunContext, StageResult, to_jsonable
from app.rules.definitions import run_confidence
from app.services.llm import iter_fields, mask_long_digit_runs
from app.services.matching import locate_evidence


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def reading_path(sha256: str):
    return config.UPLOADS_DIR / sha256 / "reading.json"


def write_reading(ctx: RunContext) -> None:
    """Everything S01-S03 learned, so a replay can re-run S05 onward without re-reading the file.

    Long digit runs are masked: the text layer contains the bank account number.
    """
    path = reading_path(ctx.sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"sha256": ctx.sha256, "mime": ctx.mime, "page_count": ctx.page_count, "file_meta": ctx.file_meta,
            "is_scanned": ctx.is_scanned, "image_only_pages": ctx.image_only_pages,
            "ocr_unavailable": ctx.ocr_unavailable,
            "legibility_by_page": {str(k): v for k, v in ctx.legibility_by_page.items()},
            "processed_page_paths": ctx.processed_page_paths,
            "text_by_page": [mask_long_digit_runs(t) for t in ctx.text_by_page],
            "ocr_text_by_page": [mask_long_digit_runs(t) for t in ctx.ocr_text_by_page],
            "word_boxes": [{**w, "text": mask_long_digit_runs(w["text"])} for w in ctx.word_boxes]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _normalised_value(path: str, inv: NormalisedInvoice) -> Any:
    """The typed value S05 produced for a field path, as stored text."""
    top = {"invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date, "due_date": inv.due_date,
           "currency": inv.currency, "supplier.name": inv.supplier_name_canonical,
           "supplier.tax_id": inv.supplier_tax_id, "totals.subtotal": inv.subtotal_minor,
           "totals.discount": inv.discount_minor, "totals.tax_total": inv.tax_total_minor,
           "totals.shipping": inv.shipping_minor, "totals.grand_total": inv.grand_total_minor,
           "payment.account_number": f"…{inv.bank_account_last4}" if inv.bank_account_last4 else None,
           "payment.ifsc_or_swift": inv.bank_ifsc}
    if path in top:
        return top[path]
    m = re.fullmatch(r"line_items\[(\d+)\]\.(\w+)", path)
    if m and int(m[1]) < len(inv.lines):
        line = inv.lines[int(m[1])]
        return {"qty": line.qty, "unit_price": line.unit_price_minor, "line_total": line.line_total_minor,
                "tax_rate_pct": line.tax_rate_pct, "sku": line.sku}.get(m[2])
    m = re.fullmatch(r"totals\.tax_breakdown\[(\d+)\]\.amount", path)
    if m and int(m[1]) < len(inv.tax_breakdown):
        return inv.tax_breakdown[int(m[1])].amount_minor
    return None


def write_fields(conn, ctx: RunContext) -> tuple[int, int]:
    conn.execute("DELETE FROM extracted_fields WHERE run_id = ?", (ctx.run_id,))
    rows, located = [], 0
    for path, f in iter_fields(ctx.extraction or {}):
        bbox = locate_evidence(f.get("evidence"), f.get("page"), ctx.word_boxes)
        located += bbox is not None
        norm = _normalised_value(path, ctx.invoice) if ctx.invoice else None
        rows.append((ctx.run_id, path, f.get("value"), None if norm is None else str(to_jsonable(norm)),
                     f.get("confidence"), f.get("evidence"), f.get("page"),
                     json.dumps(bbox) if bbox else None))
    conn.executemany("""INSERT INTO extracted_fields (run_id, field_path, value_text, value_norm, confidence,
                                                      evidence_text, page, bbox_json)
                        VALUES (?,?,?,?,?,?,?,?)""", rows)
    return len(rows), located


def run(ctx: RunContext) -> StageResult:
    d = ctx.decision
    failed = ctx.failed_stage is not None
    if not ctx.persist:
        return StageResult("skipped", "dry-run replay — nothing written", {"persist": False})

    if ctx.sha256 and ctx.page_count:
        write_reading(ctx)
    inv = ctx.invoice
    with db.session(ctx.db_path) as conn:
        n_fields, n_boxes = write_fields(conn, ctx) if ctx.extraction else (0, 0)
        finished = now_iso()
        duration = int((time.perf_counter() - ctx.t0) * 1000)   # this execution, not time spent waiting on a clerk
        conn.execute(
            """UPDATE runs SET finished_at=?, stored_path=?, mime=?, file_sha256=?, page_count=?, is_scanned=?,
                              status=?, decision=?, decision_reasons_json=?, confidence=?, matched_po_number=?,
                              matched_vendor_id=?, extracted_json=?, duration_ms=?, error=?
               WHERE id=?""",
            (finished, str(ctx.stored_path) if ctx.stored_path else None, ctx.mime, ctx.sha256, ctx.page_count,
             int(ctx.is_scanned), "failed" if failed else "completed", d.verdict if d else None,
             json.dumps(d.reason_codes) if d else None,
             run_confidence(ctx) if inv else None,
             ctx.po_match.po_number if ctx.po_match else None,
             ctx.vendor_match.vendor_id if ctx.vendor_match else None,
             json.dumps({"invoice": to_jsonable(inv), "decision": to_jsonable(d),
                         "selfcheck": ctx.selfcheck}, ensure_ascii=False) if inv or d else None,
             duration, ctx.failure_error, ctx.run_id))
        if d:
            conn.execute("""INSERT INTO audit_log (run_id, actor, action, from_value, to_value, note, created_at)
                            VALUES (?, 'system', 'decision_made', NULL, ?, ?, ?)""",
                         (ctx.run_id, d.verdict, f"{', '.join(d.reason_codes) or 'no exceptions'} — {d.explanation}",
                          finished))
    return StageResult("ok", f"run saved ({duration} ms); {n_fields} fields, {n_boxes} located on the page; "
                             f"audit entry: decision_made {d.verdict if d else '—'}",
                       {"fields": n_fields, "bboxes_located": n_boxes, "duration_ms": duration})
