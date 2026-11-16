"""S01 Ingest: real file type from magic bytes, hash, page count, validity."""
from __future__ import annotations

import hashlib
import io

import pymupdf
from PIL import Image

from app import config, db
from app.models import RunContext, StageResult

_EXT = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg",
        "image/tiff": "tif", "image/webp": "webp"}
_CLAIMABLE = {"application/pdf": {"pdf"}, "image/png": {"png"}, "image/jpeg": {"jpg", "jpeg"},
              "image/tiff": {"tif", "tiff"}, "image/webp": {"webp"}}


def sniff_mime(data: bytes) -> str | None:
    """Type from content, never from the extension: vendors do send JPEGs named .pdf."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def run(ctx: RunContext) -> StageResult:
    data = ctx.file_bytes
    size_kb = round(len(data) / 1024, 1)
    if len(data) == 0:
        return StageResult("failed", "empty file", {"bytes": 0})
    if len(data) > config.MAX_UPLOAD_BYTES:
        return StageResult("failed", f"file is {size_kb / 1024:.1f} MB; the limit is "
                                     f"{config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB", {"bytes": len(data)})
    mime = sniff_mime(data)
    if mime not in config.ACCEPTED_MIME:
        return StageResult("failed", "unsupported file type — accepted: PDF, PNG, JPEG, TIFF, WEBP",
                           {"bytes": len(data), "magic": data[:8].hex()})

    sha = hashlib.sha256(data).hexdigest()
    ext = _EXT[mime]
    claimed_ext = ctx.source_filename.rsplit(".", 1)[-1].lower() if "." in ctx.source_filename else ""
    meta: dict = {"mime": mime, "bytes": len(data), "sha256": sha, "claimed_extension": claimed_ext}
    if claimed_ext not in _CLAIMABLE[mime]:
        meta["extension_mismatch"] = f".{claimed_ext or '(none)'} file is actually {mime}"

    if mime == "application/pdf":
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except (pymupdf.FileDataError, RuntimeError) as exc:
            return StageResult("failed", f"PDF could not be opened: {exc}", meta)
        meta["encrypted"] = bool(doc.needs_pass)
        if doc.needs_pass and not doc.authenticate(""):
            return StageResult("failed", "PDF is password-protected (empty password did not open it)", meta)
        page_count = doc.page_count
        meta["pages"] = [{"page": i + 1,
                          "width_pt": round(p.rect.width, 1), "height_pt": round(p.rect.height, 1),
                          "width_px_at_raster_dpi": round(p.rect.width / 72 * config.RASTER_DPI),
                          "height_px_at_raster_dpi": round(p.rect.height / 72 * config.RASTER_DPI)}
                         for i, p in enumerate(doc)]
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()                     # catches truncated / corrupt images
            img = Image.open(io.BytesIO(data))
        except (OSError, SyntaxError) as exc:
            return StageResult("failed", f"image could not be decoded: {exc}", meta)
        page_count = getattr(img, "n_frames", 1)
        exif = img.getexif()
        meta["pages"] = [{"page": 1, "width_px": img.width, "height_px": img.height}]
        meta["dpi"] = [round(float(x)) for x in img.info["dpi"]] if "dpi" in img.info else None
        meta["exif_orientation"] = exif.get(0x0112)  # 1 = upright; 6/8 = rotated phone photo

    if page_count == 0:
        return StageResult("failed", "document has no pages", meta)
    if page_count > config.MAX_PAGES:
        return StageResult("failed", f"{page_count} pages; the limit is {config.MAX_PAGES} "
                                     "(a statement or bundle, not a single invoice)", meta)

    stored = config.UPLOADS_DIR / f"{sha}.{ext}"
    if not stored.exists():               # content-addressed: identical uploads share one file
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(data)

    ctx.sha256, ctx.mime, ctx.stored_path, ctx.page_count = sha, mime, stored, page_count
    ctx.file_meta = meta
    if ctx.persist:
        # Register the file on the run now, not at S10, so the viewer can show
        # the page while the later stages are still running.
        with db.session(ctx.db_path) as conn:
            conn.execute("UPDATE runs SET stored_path = ?, mime = ?, file_sha256 = ?, page_count = ? WHERE id = ?",
                         (str(stored), mime, sha, page_count, ctx.run_id))
    kind = "PDF" if mime == "application/pdf" else mime.split("/")[1].upper()
    note = f" ({meta['extension_mismatch']})" if "extension_mismatch" in meta else ""
    status = "warn" if note else "ok"
    return StageResult(status, f"{kind}, {page_count} page{'s' if page_count != 1 else ''}, "
                               f"{size_kb:g} KB, sha256 {sha[:10]}…{note}", meta)
