"""S04 Extract: one Gemini call per document -> schema-valid structured reading.

Pages go in as images, the text layer / OCR text goes in as a labelled,
possibly-imperfect machine reading. The output is a forced tool call validated
against the extraction schema. The model never sees the vendor master, the POs
or the rules — it cannot decide anything, only read.
"""
from __future__ import annotations

import json

from app import config, db
from app.models import RunContext, StageResult
from app.services import llm, ocr


def _machine_text(ctx: RunContext) -> str:
    parts = []
    for i in range(ctx.page_count or 0):
        text = (ctx.text_by_page[i] if i < len(ctx.text_by_page) else "") or \
               (ctx.ocr_text_by_page[i] if i < len(ctx.ocr_text_by_page) else "")
        source = "text layer" if i < len(ctx.text_by_page) and ctx.text_by_page[i] else "OCR"
        parts.append(f"--- page {i + 1} ({source}) ---\n{text.strip() or '(nothing read)'}")
    return "\n".join(parts)


def run(ctx: RunContext) -> StageResult:
    if not ctx.page_images:
        ctx.page_images = ocr.rasterise(ctx.file_bytes, ctx.mime, dpi=config.LLM_RASTER_DPI)
    images = [ocr.to_llm_jpeg(p) for p in ctx.page_images]

    try:
        result = llm.extract_document(ctx.sha256, images, _machine_text(ctx))
    except llm.ExtractionError as exc:
        return StageResult("failed", str(exc), {"mode": config.LLM_CACHE_MODE, "model": config.LLM_MODEL})

    # Persist the verbatim model output before anything else touches it: this is
    # the evidence that the decision was made by code, not by the model.
    if ctx.persist:
        with db.session(ctx.db_path) as conn:
            conn.execute("UPDATE runs SET raw_llm_json = ? WHERE id = ?",
                         (json.dumps(result.raw, ensure_ascii=False), ctx.run_id))

    ctx.extraction, ctx.raw_llm, ctx.extraction_source = result.payload, result.raw, result.source
    fields = [{"field": path, "value": f.get("value"), "confidence": f.get("confidence"),
               "evidence": f.get("evidence"), "page": f.get("page")}
              for path, f in llm.iter_fields(result.payload)]
    present = [f for f in fields if f["value"] is not None]
    lowest = min((f for f in present), key=lambda f: f["confidence"], default=None)
    detail = {
        "source": result.source, "model": result.raw.get("model"), "mode": config.LLM_CACHE_MODE,
        "prompt_version": result.raw.get("prompt_version"), "attempts": result.attempts,
        "usage": result.usage, "pages_sent": len(images),
        "image_bytes": [len(b) for b in images],
        "document_type": result.payload["document_type"],
        "document_quality": result.payload["document_quality"],
        "notes": result.payload["notes"], "unreadable_regions": result.payload["unreadable_regions"],
        "fields": fields,
    }
    src = "cache hit" if result.source == "cache" else f"live, {result.usage['input_tokens']}+{result.usage['output_tokens']} tokens"
    summary = (f"{len(present)} of {len(fields)} fields read ({src})"
               + (f"; lowest confidence {lowest['confidence']:.2f} on {lowest['field']}" if lowest else ""))
    return StageResult("ok", summary, detail)
