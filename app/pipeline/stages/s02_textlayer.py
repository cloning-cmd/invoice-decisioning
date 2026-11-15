"""S02 Text layer: native PDF text + word boxes, and a coverage score per page.

Coverage = non-whitespace characters per square inch. A real digital invoice
is dense; a scanned page wrapped in a PDF has (almost) no text at all. Below
TEXT_COVERAGE_MIN the page is treated as image-only and routed to OCR.
"""
from __future__ import annotations

import pymupdf

from app import config
from app.models import RunContext, StageResult


def run(ctx: RunContext) -> StageResult:
    if ctx.mime != "application/pdf":
        ctx.is_scanned = True
        return StageResult("skipped", "image input — no text layer; routed to OCR",
                           {"reason": "image input"})

    doc = pymupdf.open(stream=ctx.file_bytes, filetype="pdf")
    if doc.needs_pass:
        doc.authenticate("")
    pages, texts, boxes, image_only = [], [], [], []
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        w_in, h_in = page.rect.width / 72, page.rect.height / 72
        chars = sum(1 for c in text if not c.isspace())
        coverage = chars / (w_in * h_in)
        digital = coverage >= config.TEXT_COVERAGE_MIN
        texts.append(text if digital else "")
        if digital:
            # (x0, y0, x1, y1, word, block, line, word_no) in points -> normalised 0-1
            for x0, y0, x1, y1, word, *_ in page.get_text("words"):
                boxes.append({"text": word, "conf": 100.0, "page": i, "source": "textlayer",
                              "bbox": [x0 / page.rect.width, y0 / page.rect.height,
                                       x1 / page.rect.width, y1 / page.rect.height]})
        else:
            image_only.append(i)
        pages.append({"page": i, "chars": chars, "coverage_chars_per_sq_in": round(coverage, 1),
                      "digital": digital})

    ctx.text_by_page, ctx.word_boxes, ctx.image_only_pages = texts, boxes, image_only
    ctx.is_scanned = bool(image_only)
    detail = {"pages": pages, "threshold_chars_per_sq_in": config.TEXT_COVERAGE_MIN,
              "words": len(boxes), "image_only_pages": image_only}
    n = len(pages)
    if not image_only:
        return StageResult("ok", f"text layer on all {n} page{'s' if n > 1 else ''}, {len(boxes)} words "
                                 f"(coverage {pages[0]['coverage_chars_per_sq_in']:g} chars/sq.in)", detail)
    if len(image_only) == n:
        return StageResult("ok", f"no usable text layer (coverage {pages[0]['coverage_chars_per_sq_in']:g} "
                                 f"< {config.TEXT_COVERAGE_MIN:g} chars/sq.in) — image-only PDF, routed to OCR",
                           detail)
    return StageResult("warn", f"mixed document: page(s) {', '.join(map(str, image_only))} are image-only "
                               f"and go to OCR; {len(boxes)} words from the rest", detail)
