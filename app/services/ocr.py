"""Page rasterisation and the tesseract wrapper.

Phase 1 holds rasterisation (every stage downstream works on PIL images,
whatever the input format) and the availability probe. Preprocessing and
word-box OCR arrive with the scanner path in Phase 3.
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image, ImageOps

from app import config

# Default install location of the UB-Mannheim Windows build, which is not added to PATH.
_WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

INSTALL_HINT = ("tesseract not found. Install it: macOS `brew install tesseract`, "
                "Debian/Ubuntu `sudo apt-get install tesseract-ocr`, Windows "
                "`winget install UB-Mannheim.TesseractOCR` (or set TESSERACT_CMD).")


def _configure_binary() -> None:
    if config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
    elif shutil.which("tesseract") is None and _WINDOWS_DEFAULT.exists():
        pytesseract.pytesseract.tesseract_cmd = str(_WINDOWS_DEFAULT)


def tesseract_version() -> str | None:
    """Installed tesseract version, or None if the binary cannot be run."""
    _configure_binary()
    try:
        return str(pytesseract.get_tesseract_version())
    except (pytesseract.TesseractNotFoundError, OSError):
        return None


def rasterise(file_bytes: bytes, mime: str, dpi: int | None = None) -> list[Image.Image]:
    """Every input format -> a list of RGB PIL pages, EXIF orientation applied."""
    if mime == "application/pdf":
        doc = pymupdf.open(stream=file_bytes, filetype="pdf")
        if doc.needs_pass:
            doc.authenticate("")
        zoom = (dpi or config.RASTER_DPI) / 72.0
        pages = []
        for page in doc:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
        return pages
    img = Image.open(io.BytesIO(file_bytes))
    pages = []
    for i in range(getattr(img, "n_frames", 1)):   # multi-page TIFF
        img.seek(i)
        frame = ImageOps.exif_transpose(img) if config.PREPROCESS_EXIF_ROTATE else img
        pages.append(frame.convert("RGB"))
    return pages


def to_llm_jpeg(page: Image.Image) -> bytes:
    """Downscale so the longest side fits the model's native resolution; JPEG-encode."""
    img = page.copy()
    img.thumbnail((config.LLM_IMAGE_MAX_SIDE_PX, config.LLM_IMAGE_MAX_SIDE_PX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=config.LLM_IMAGE_JPEG_QUALITY, optimize=True)
    return buf.getvalue()
