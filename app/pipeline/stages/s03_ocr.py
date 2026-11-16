"""S03 OCR: the scanner path. Runs only when the document is (partly) scanned.

1. Rasterise every page at RASTER_DPI (images load direct, EXIF applied).
2. Preprocess the image-only pages: greyscale, deskew, shadow removal,
   denoise (if noisy), adaptive threshold, upscale (if small). Each processed
   page is saved as data/uploads/<sha>/p<N>_proc.png for the before/after view.
3. OCR with tesseract, PSM 6 then PSM 4 if confidence is low; keep the better.
4. Word boxes, mapped back to the original page and normalised 0-1.
5. Legibility per page = length-weighted mean word confidence; below
   OCR_LEGIBILITY_MIN the page is degraded and the stage warns (feeds R13).

Without tesseract the stage still preprocesses (so the before/after view
works), then warns and lets S04 read the page images vision-only.
"""
from __future__ import annotations

from app import config
from app.models import RunContext, StageResult
from app.services import ocr
from app.services.ocr import processed_page_path


def run(ctx: RunContext) -> StageResult:
    if not ctx.is_scanned:
        return StageResult("skipped", "digital text layer present — OCR not needed", {"reason": "not scanned"})

    pages = ocr.rasterise(ctx.file_bytes, ctx.mime, dpi=config.RASTER_DPI)
    ctx.page_images = pages            # S04 sends these to the model, downscaled
    targets = ctx.image_only_pages if ctx.mime == "application/pdf" else list(range(1, len(pages) + 1))
    tesseract = ocr.tesseract_version()
    ctx.ocr_unavailable = tesseract is None
    ctx.ocr_text_by_page = [""] * len(pages)
    ctx.processed_page_paths = []
    boxes = list(ctx.word_boxes)       # text-layer boxes of any digital pages stay

    page_rows, steps = [], []
    for n in targets:
        pre = ocr.preprocess(pages[n - 1])
        path = processed_page_path(ctx.sha256, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ocr.to_png(pre.image))
        ctx.processed_page_paths.append(str(path))
        steps += [{"page": n, **s} for s in pre.steps]
        row = {"page": n, "processed": True, "skew_out_of_range": pre.skew_out_of_range,
               "processed_size": f"{pre.image.shape[1]}x{pre.image.shape[0]}px"}
        if tesseract is not None:
            r = ocr.ocr_page(pre.image)
            ctx.ocr_text_by_page[n - 1] = r.text
            ctx.legibility_by_page[n] = r.legibility
            for w in r.words:
                boxes.append({"text": w["text"], "conf": w["conf"], "page": n, "source": "ocr",
                              "bbox": list(pre.to_original(*w["box"]))})
            row.update({"legibility": r.legibility, "psm": r.psm, "mean_conf": round(r.mean_conf, 1),
                        "words": len(r.words), "tried": r.tried,
                        "degraded": r.legibility < config.OCR_LEGIBILITY_MIN or pre.skew_out_of_range})
        page_rows.append(row)
    ctx.word_boxes = boxes

    detail = {"pages": page_rows, "steps": steps, "legibility_min": config.OCR_LEGIBILITY_MIN,
              "raster_dpi": config.RASTER_DPI, "tesseract": tesseract}
    plural = "s" if len(targets) != 1 else ""
    if tesseract is None:
        detail["install"] = ocr.INSTALL_HINT
        return StageResult("warn", f"tesseract not installed — {len(targets)} page{plural} preprocessed for display; "
                                   "extraction proceeds vision-only", detail)

    applied = [s["step"] for s in steps if s["applied"] and s["step"] not in ("greyscale",)]
    readings = ", ".join(f"p{r['page']} {r['legibility']:.2f} (PSM {r['psm']})" for r in page_rows)
    degraded = [r for r in page_rows if r["degraded"]]
    summary = (f"OCR on {len(targets)} page{plural}: legibility {readings}; "
               f"{sum(r['words'] for r in page_rows)} words; {', '.join(dict.fromkeys(applied)) or 'no preprocessing'}")
    if degraded:
        why = "; ".join(f"page {r['page']} " + ("skew beyond correction" if r["skew_out_of_range"]
                                                else f"legibility {r['legibility']:.2f} < {config.OCR_LEGIBILITY_MIN}")
                        for r in degraded)
        return StageResult("warn", f"{summary} — degraded: {why}", detail)
    return StageResult("ok", summary, detail)
