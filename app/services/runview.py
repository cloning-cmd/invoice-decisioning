"""Read model for the UI: a run as the screens need it, rebuilt from SQLite.

The same functions serve the live stream's final event, a refresh after the
run has finished, the run-detail screen and the JSON export, so a run looks
identical whether you watched it or open it three days later.
"""
from __future__ import annotations

import io
import json
import os
import secrets
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageOps

from app import config, db
from app.pipeline.stages.s09_decide import VERDICT_WORDS
from app.services.money import format_money


class NotFound(LookupError):
    pass


def _loads(text: str | None, default: Any = None) -> Any:
    return json.loads(text) if text else default


def _run_row(conn, run_id: str) -> dict[str, Any]:
    run = db.query_one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if run is None:
        raise NotFound(f"no run {run_id}")
    return run


def _stages(conn, run_id: str) -> list[dict[str, Any]]:
    rows = db.query(conn, "SELECT run_id, stage_key, stage_label, seq, status, started_at, ended_at, duration_ms, "
                          "summary, detail_json, error FROM run_stages WHERE run_id = ? ORDER BY seq", (run_id,))
    for r in rows:
        r["detail"] = _loads(r.pop("detail_json"), {})
    return rows


# ---------------------------------------------------------------- decision card

def card(conn, run_id: str) -> dict[str, Any] | None:
    """Everything the decision card shows, or None if the run never reached a decision."""
    run = _run_row(conn, run_id)
    extracted = _loads(run["extracted_json"], {})
    d = extracted.get("decision")
    if not d:
        return None
    inv = extracted.get("invoice") or {}
    match = db.query_one(conn, "SELECT detail_json FROM run_stages WHERE run_id = ? AND stage_key = 'match'",
                         (run_id,))
    mdetail = _loads(match["detail_json"], {}) if match else {}
    cons = mdetail.get("consumption")
    pm = mdetail.get("po") or {}

    cur = inv.get("currency") or "INR"
    total = inv.get("grand_total_minor")
    po_number = run["matched_po_number"] or pm.get("quoted_po_number")
    po = db.query_one(conn, "SELECT currency FROM purchase_orders WHERE po_number = ?", (po_number,)) \
        if po_number else None
    po_cur = po["currency"] if po else cur
    remaining = cons["remaining_minor"] if cons else None
    variance = (total - remaining) if (total is not None and remaining is not None and po_cur == cur) else None
    conf = run["confidence"]

    figures = [
        {"key": "total", "label": "Invoice total", "value": format_money(total, cur)},
        {"key": "po", "label": "Matched PO" if run["matched_po_number"] else "Quoted PO",
         "value": po_number or "none"},
        {"key": "remaining", "label": "PO remaining", "value": format_money(remaining, po_cur)},
        {"key": "variance", "label": "Variance vs remaining",
         "value": (f"{format_money(abs(variance), cur)} {'over' if variance > 0 else 'under'}" if variance
                   else format_money(0, cur)) if variance is not None
                  else ("currency differs" if remaining is not None else "—"),
         "tone": "fail" if variance is not None and variance > 0 else None},
        {"key": "confidence", "label": "Confidence", "value": f"{conf:.2f}" if conf is not None else "—"},
    ]
    return {**d, "verdict_words": VERDICT_WORDS[d["verdict"]], "figures": figures,
            "override_roles": list(config.OVERRIDE_ROLES[d["verdict"]]),
            "currency": cur, "total_minor": total}


# ---------------------------------------------------------------- replay for SSE

def stored_events(conn, run_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The event sequence a live subscriber would have seen, rebuilt from the database."""
    run = _run_row(conn, run_id)
    events: list[tuple[str, dict[str, Any]]] = []
    for s in _stages(conn, run_id):
        if s["status"] == "pending":
            continue
        events.append(("stage_started" if s["status"] == "running" else "stage_finished", s))
    if run["status"] in ("completed", "failed"):
        decision = _loads(run["extracted_json"], {}).get("decision")
        events.append(("run_failed" if run["status"] == "failed" else "run_finished",
                       {"run_id": run_id, "status": run["status"], "decision": decision, "error": run["error"],
                        "card": card(conn, run_id)}))
    return events


# ---------------------------------------------------------------- full record

def record(conn, run_id: str, include_raw: bool = False) -> dict[str, Any]:
    run = _run_row(conn, run_id)
    raw = run.pop("raw_llm_json")
    extracted = _loads(run.pop("extracted_json"), {})
    run["decision_reasons"] = _loads(run.pop("decision_reasons_json"), [])
    rules = db.query(conn, "SELECT rule_code, rule_name, category, severity, status, message, evidence_json "
                           "FROM rule_results WHERE run_id = ? ORDER BY rule_code", (run_id,))
    for r in rules:
        r["evidence"] = _loads(r.pop("evidence_json"), {})
    fields = db.query(conn, "SELECT field_path, value_text, value_norm, confidence, evidence_text, page, bbox_json "
                            "FROM extracted_fields WHERE run_id = ? ORDER BY id", (run_id,))
    for f in fields:
        f["bbox"] = _loads(f.pop("bbox_json"))
    audit = db.query(conn, "SELECT actor, action, from_value, to_value, note, created_at FROM audit_log "
                           "WHERE run_id = ? ORDER BY id", (run_id,))
    out = {"run": run, "invoice": extracted.get("invoice"), "selfcheck": extracted.get("selfcheck"),
           "stages": _stages(conn, run_id), "rules": rules, "fields": fields, "audit": audit,
           "card": card(conn, run_id), "has_raw_llm": raw is not None}
    if include_raw:
        out["raw_llm_json"] = _loads(raw)
    return out


# ---------------------------------------------------------------- page images

def processed_page_path(sha256: str, page: int) -> Path:
    """Where S03 writes the preprocessed raster of a page (1-based)."""
    return config.UPLOADS_DIR / sha256 / f"processed-{page}.png"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def page_image(conn, run_id: str, page: int, variant: str) -> tuple[Path, str]:
    """(file, media type) for a rendered page. Original renders are cached next to the upload."""
    run = _run_row(conn, run_id)
    if not run["stored_path"] or not run["file_sha256"]:
        raise NotFound("this run has no stored file yet (ingest has not finished, or it failed)")
    count = run["page_count"] or 0
    if not 1 <= page <= count:
        raise NotFound(f"page {page} does not exist; the document has {count} page(s)")
    sha, mime = run["file_sha256"], run["mime"]

    if variant == "processed":
        path = processed_page_path(sha, page)
        if not path.exists():
            raise NotFound("no preprocessed image for this page (digital PDFs are not preprocessed)")
        return path, "image/png"
    if variant != "original":
        raise ValueError("variant must be 'original' or 'processed'")

    is_pdf = mime == "application/pdf"
    path = config.UPLOADS_DIR / sha / f"view-{page}.{'png' if is_pdf else 'jpg'}"
    if path.exists():
        return path, "image/png" if is_pdf else "image/jpeg"
    data = Path(run["stored_path"]).read_bytes()
    if is_pdf:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            doc.authenticate("")
        zoom = config.VIEWER_RASTER_DPI / 72.0
        pix = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        _write_atomic(path, pix.tobytes("png"))
        return path, "image/png"
    img = Image.open(io.BytesIO(data))
    img.seek(page - 1)
    # Same orientation the pipeline read (EXIF applied), so bounding boxes line up.
    frame = (ImageOps.exif_transpose(img) if config.PREPROCESS_EXIF_ROTATE else img).convert("RGB")
    buf = io.BytesIO()
    frame.save(buf, "JPEG", quality=config.VIEWER_JPEG_QUALITY)
    _write_atomic(path, buf.getvalue())
    return path, "image/jpeg"
