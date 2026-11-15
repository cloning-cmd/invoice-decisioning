"""Tesseract wrapper. Phase 0 holds only the availability probe; preprocessing
and word-box extraction arrive with the scanner path in Phase 3."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytesseract

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
