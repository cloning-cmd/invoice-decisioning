"""Orchestrates the ten stages: runs them in order, records every status
transition in run_stages, and emits an event at each transition.

A failed hard stage aborts the run: the remaining stages are marked skipped,
the run ends as status=failed with decision HOLD_FOR_REVIEW and a
STAGE_FAILED reason, and S10 still runs so the failure itself is persisted.
Text layer and OCR are soft: if they fail, extraction proceeds vision-only.
"""
from __future__ import annotations

import json
import secrets
import time
import traceback
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from app import config, db
from app.models import (Decision, GoodsReceipt, POLine, PurchaseOrder, ReferenceData, RunContext, StageResult,
                        Vendor, to_jsonable)
from app.pipeline.stages import (s01_ingest, s02_textlayer, s03_ocr, s04_extract, s05_normalise, s06_selfcheck,
                                 s07_match, s08_rules, s09_decide, s10_persist)

STAGES = [
    ("ingest", "Ingest", s01_ingest),
    ("textlayer", "Text layer", s02_textlayer),
    ("ocr", "OCR", s03_ocr),
    ("extract", "Extract", s04_extract),
    ("normalise", "Normalise", s05_normalise),
    ("selfcheck", "Self-check", s06_selfcheck),
    ("match", "Match", s07_match),
    ("rules", "Rules", s08_rules),
    ("decide", "Decide", s09_decide),
    ("persist", "Persist", s10_persist),
]
SEQ = {key: i for i, (key, _, _) in enumerate(STAGES, start=1)}
SOFT_STAGES = {"textlayer", "ocr"}   # failure degrades the reading, it does not stop the run

Emit = Callable[[str, dict[str, Any]], None]


def _noop(event: str, payload: dict[str, Any]) -> None:
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    """Sortable by creation time, unique enough for one process."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:18] + "-" + secrets.token_hex(3)


# ---------------------------------------------------------------- reference data

def load_reference(db_path: Path | None = None) -> ReferenceData:
    with db.session(db_path) as conn:
        vendors = {r["id"]: Vendor(r["id"], r["legal_name"], tuple(json.loads(r["aliases_json"])), r["status"],
                                   r["tax_id"], r["bank_account_last4"], r["bank_ifsc"], r["payment_terms_days"],
                                   r["default_currency"])
                   for r in db.query(conn, "SELECT * FROM vendors")}
        lines: dict[str, list[POLine]] = {}
        for r in db.query(conn, "SELECT * FROM po_line_items ORDER BY id"):
            lines.setdefault(r["po_id"], []).append(
                POLine(r["sku"], r["description"], Decimal(str(r["qty"])), r["uom"], r["unit_price_minor"],
                       r["line_total_minor"]))
        pos = {r["po_number"]: PurchaseOrder(
                   r["id"], r["po_number"], r["vendor_id"], r["currency"], r["po_total_minor"], r["status"],
                   bool(r["allow_partial_invoicing"]), date.fromisoformat(r["issue_date"]),
                   date.fromisoformat(r["expiry_date"]) if r["expiry_date"] else None,
                   tuple(lines.get(r["id"], [])))
               for r in db.query(conn, "SELECT * FROM purchase_orders")}
        grns: dict[str, list[GoodsReceipt]] = {}
        for r in db.query(conn, "SELECT * FROM goods_receipts ORDER BY received_date"):
            grns.setdefault(r["po_id"], []).append(GoodsReceipt(
                r["id"], r["po_id"], r["grn_number"], date.fromisoformat(r["received_date"]),
                tuple((l["sku"], Decimal(str(l["qty_received"]))) for l in json.loads(r["lines_json"]))))
    return ReferenceData(vendors, pos, {k: tuple(v) for k, v in grns.items()})


# ---------------------------------------------------------------- run rows

def create_run(source_filename: str, db_path: Path | None = None, run_id: str | None = None) -> str:
    """Insert the run and all ten stage rows as pending, so the UI can draw the full timeline at once."""
    run_id = run_id or new_run_id()
    with db.session(db_path) as conn:
        conn.execute("INSERT INTO runs (id, created_at, source_filename, status) VALUES (?,?,?, 'queued')",
                     (run_id, now_iso(), source_filename))
        conn.executemany("INSERT INTO run_stages (run_id, stage_key, stage_label, seq, status) "
                         "VALUES (?,?,?,?, 'pending')",
                         [(run_id, key, label, SEQ[key]) for key, label, _ in STAGES])
    return run_id


def stage_row(ctx: RunContext, key: str) -> dict[str, Any] | None:
    with db.session(ctx.db_path) as conn:
        row = db.query_one(conn, "SELECT * FROM run_stages WHERE run_id = ? AND stage_key = ?", (ctx.run_id, key))
    if row and row.get("detail_json"):
        row["detail"] = json.loads(row.pop("detail_json"))
    return row


def _update_stage(ctx: RunContext, key: str, **cols: Any) -> None:
    if not ctx.persist:
        return
    sets = ", ".join(f"{c} = ?" for c in cols)   # column names are literals from this module
    with db.session(ctx.db_path) as conn:
        conn.execute(f"UPDATE run_stages SET {sets} WHERE run_id = ? AND stage_key = ?",
                     (*cols.values(), ctx.run_id, key))


# ---------------------------------------------------------------- execution

def _call_stage(module: Any, ctx: RunContext) -> StageResult:
    try:
        return module.run(ctx)
    except Exception as exc:  # noqa: BLE001 — recorded as a failed stage with its traceback, never swallowed
        return StageResult("failed", f"{type(exc).__name__}: {exc}",
                           {"traceback": traceback.format_exc(limit=8)})


def execute(ctx: RunContext, emit: Emit = _noop, start_at: str = "ingest") -> RunContext:
    """Run stages from `start_at` to the end. Every transition is persisted and emitted."""
    ctx.t0 = time.perf_counter()
    if ctx.ref is None:
        ctx.ref = load_reference(ctx.db_path)
    if ctx.persist:
        with db.session(ctx.db_path) as conn:
            conn.execute("UPDATE runs SET status = 'running', finished_at = NULL, error = NULL WHERE id = ?",
                         (ctx.run_id,))
    aborted = False
    for key, label, module in STAGES:
        seq = SEQ[key]
        if seq < SEQ[start_at]:
            continue
        base = {"run_id": ctx.run_id, "stage_key": key, "stage_label": label, "seq": seq}
        if aborted and key != "persist":
            row = {**base, "status": "skipped", "summary": "not run: an earlier stage failed",
                   "started_at": None, "ended_at": None, "duration_ms": None, "detail": {}, "error": None}
            _update_stage(ctx, key, status="skipped", summary=row["summary"], started_at=None, ended_at=None,
                          duration_ms=None, detail_json=None, error=None)
            emit("stage_finished", row)
            continue

        started = now_iso()
        _update_stage(ctx, key, status="running", started_at=started, ended_at=None, duration_ms=None,
                      summary=None, detail_json=None, error=None)
        emit("stage_started", {**base, "status": "running", "started_at": started})

        t = time.perf_counter()
        result = _call_stage(module, ctx)
        ms = int((time.perf_counter() - t) * 1000)
        detail = to_jsonable(result.detail)
        error = result.summary if result.status == "failed" else None
        ended = now_iso()
        _update_stage(ctx, key, status=result.status, ended_at=ended, duration_ms=ms, summary=result.summary,
                      detail_json=json.dumps(detail, ensure_ascii=False), error=error)
        emit("stage_finished", {**base, "status": result.status, "started_at": started, "ended_at": ended,
                                "duration_ms": ms, "summary": result.summary, "detail": detail, "error": error})

        if result.status == "warn":
            ctx.stage_warnings[key] = result.summary
        if result.status == "failed" and key not in SOFT_STAGES and key != "persist":
            aborted = True
            ctx.failed_stage, ctx.failure_error = key, f"{label} failed: {result.summary}"
            ctx.decision = Decision(
                "HOLD_FOR_REVIEW", [f"STAGE_FAILED:{key}"],
                f"The {label} stage failed: {result.summary}. No automated decision was made.",
                "The pipeline could not finish; process this invoice manually",
                config.OWNERS["ap_clerk"], "stage failure → HOLD_FOR_REVIEW")
        if result.status == "failed" and key == "persist" and ctx.persist:
            # Last resort: S10 itself failed, so record the failure directly.
            with db.session(ctx.db_path) as conn:
                conn.execute("UPDATE runs SET status='failed', finished_at=?, error=? WHERE id=?",
                             (ended, f"Persist failed: {result.summary}", ctx.run_id))
            ctx.failed_stage, ctx.failure_error = key, result.summary

    final = {"run_id": ctx.run_id, "status": "failed" if ctx.failed_stage else "completed",
             "decision": to_jsonable(ctx.decision), "error": ctx.failure_error}
    emit("run_failed" if ctx.failed_stage else "run_finished", final)
    return ctx


def run_file(file_bytes: bytes, filename: str, *, db_path: Path | None = None, emit: Emit = _noop,
             today: date | None = None, run_id: str | None = None) -> RunContext:
    """Process one uploaded file end to end."""
    run_id = run_id or create_run(filename, db_path)
    ctx = RunContext(run_id=run_id, db_path=db_path, today=today or config.as_of_date(),
                     source_filename=filename, file_bytes=file_bytes)
    return execute(ctx, emit)


def replay(run_id: str, *, db_path: Path | None = None, overrides: dict[str, str] | None = None,
           persist: bool = False, emit: Emit = _noop, today: date | None = None) -> RunContext:
    """Re-run S05 onward against the stored extraction. Never calls the model.

    persist=False is a dry run (the CLI's --replay): it shows what the current
    config would decide without rewriting history. persist=True is the
    supply-missing-field path, which updates the same run.
    """
    with db.session(db_path) as conn:
        run = db.query_one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if run is None:
        raise ValueError(f"no run {run_id}")
    if not run["raw_llm_json"]:
        raise ValueError(f"run {run_id} has no stored extraction to replay (its extract stage never succeeded)")
    raw = json.loads(run["raw_llm_json"])
    reading_file = s10_persist.reading_path(run["file_sha256"])
    if not reading_file.exists():
        raise ValueError(f"reading artefact missing for run {run_id}: {reading_file}")
    reading = json.loads(reading_file.read_text(encoding="utf-8"))

    ctx = RunContext(run_id=run_id, db_path=db_path, today=today or config.as_of_date(),
                     source_filename=run["source_filename"], file_bytes=Path(run["stored_path"]).read_bytes(),
                     persist=persist, overrides=dict(overrides or {}))
    ctx.sha256, ctx.mime, ctx.stored_path = run["file_sha256"], run["mime"], Path(run["stored_path"])
    ctx.page_count, ctx.file_meta = reading["page_count"], reading["file_meta"]
    ctx.is_scanned, ctx.image_only_pages = reading["is_scanned"], reading["image_only_pages"]
    ctx.ocr_unavailable = reading["ocr_unavailable"]
    ctx.legibility_by_page = {int(k): v for k, v in reading["legibility_by_page"].items()}
    ctx.processed_page_paths = reading["processed_page_paths"]
    ctx.text_by_page, ctx.ocr_text_by_page = reading["text_by_page"], reading["ocr_text_by_page"]
    ctx.word_boxes = reading["word_boxes"]
    ctx.extraction, ctx.raw_llm, ctx.extraction_source = raw["payload"], raw, "replay"
    return execute(ctx, emit, start_at="normalise")
