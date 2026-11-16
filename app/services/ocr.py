"""Page rasterisation, scan preprocessing (opencv) and the tesseract wrapper.

Every input format becomes a list of RGB PIL pages, so downstream stages are
format-agnostic. Preprocessing is a fixed sequence of named, individually
toggleable steps (config.PREPROCESS_*); each step records what it did so the
UI can show it. Word boxes are reported in the coordinates of the ORIGINAL
page (the deskew rotation and the upscale are undone), so the overlay lands on
the image the clerk is looking at.
"""
from __future__ import annotations

import io
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
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


def processed_page_path(sha256: str, page: int) -> Path:
    """Where S03 writes the preprocessed raster of a page (1-based)."""
    return config.UPLOADS_DIR / sha256 / f"p{page}_proc.png"


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


# ---------------------------------------------------------------- preprocessing

@dataclass
class Preprocessed:
    image: np.ndarray                          # the binarised page tesseract reads (uint8, 1 channel)
    original_size: tuple[int, int]             # (width, height) of the page before preprocessing
    steps: list[dict[str, Any]] = field(default_factory=list)
    rotation: np.ndarray | None = None         # 2x3 affine applied by deskew, None if not rotated
    scale: float = 1.0                         # upscale factor applied last
    skew_out_of_range: bool = False            # |angle| beyond the cap: a photo problem, flagged not fixed

    def to_original(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        """Map a box on the processed image back to the original page, normalised 0-1."""
        pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64) / self.scale
        if self.rotation is not None:
            inv = cv2.invertAffineTransform(self.rotation)
            pts = np.hstack([pts, np.ones((4, 1))]) @ inv.T
        w, h = self.original_size
        xs, ys = np.clip(pts[:, 0] / w, 0, 1), np.clip(pts[:, 1] / h, 0, 1)
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def estimate_skew(gray: np.ndarray) -> tuple[float | None, int]:
    """Median angle (degrees) of long near-horizontal lines, and how many lines voted."""
    edges = cv2.Canny(gray, config.DESKEW_CANNY_LOW, config.DESKEW_CANNY_HIGH)
    min_len = int(gray.shape[1] * config.DESKEW_HOUGH_MIN_LINE_FRAC)
    lines = cv2.HoughLinesP(edges, 1, math.pi / 180, config.DESKEW_HOUGH_THRESHOLD,
                            minLineLength=min_len, maxLineGap=config.DESKEW_HOUGH_MAX_GAP_PX)
    if lines is None:
        return None, 0
    angles = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):   # (N,1,4) in OpenCV 4, (N,4) in 5
        a = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if a > 90:
            a -= 180
        elif a < -90:
            a += 180
        if abs(a) <= config.DESKEW_NEAR_HORIZONTAL_DEG:
            angles.append(a)
    if not angles:
        return None, 0
    return float(np.median(angles)), len(angles)


def estimate_noise(gray: np.ndarray) -> float:
    """Immerkaer's fast noise sigma estimate (Laplacian-difference kernel)."""
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float64)
    conv = cv2.filter2D(gray.astype(np.float64), -1, kernel)
    h, w = gray.shape
    return float(np.sum(np.abs(conv[1:-1, 1:-1])) * math.sqrt(math.pi / 2) / (6 * (w - 2) * (h - 2)))


def preprocess(page: Image.Image) -> Preprocessed:
    """a. greyscale (EXIF already applied by rasterise)  b. deskew  c. shadow removal
    d. denoise if noisy  e. adaptive threshold  f. upscale small pages."""
    rgb = np.asarray(page.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    out = Preprocessed(gray, (w, h))
    out.steps.append({"step": "greyscale", "applied": True, "detail": f"{w}x{h}px"})

    if config.PREPROCESS_DESKEW:
        angle, votes = estimate_skew(gray)
        if angle is None:
            out.steps.append({"step": "deskew", "applied": False, "detail": "no long horizontal lines to measure"})
        elif abs(angle) > config.DESKEW_MAX_ANGLE_DEG:
            out.skew_out_of_range = True
            out.steps.append({"step": "deskew", "applied": False,
                              "detail": f"{angle:+.2f} deg exceeds the {config.DESKEW_MAX_ANGLE_DEG:g} deg cap; "
                                        "flagged, not rotated"})
        elif abs(angle) < config.DESKEW_MIN_ANGLE_DEG:
            out.steps.append({"step": "deskew", "applied": False,
                              "detail": f"{angle:+.2f} deg is under {config.DESKEW_MIN_ANGLE_DEG:g} deg; left as is"})
        else:
            m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            gray = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            out.rotation = m
            out.steps.append({"step": "deskew", "applied": True,
                              "detail": f"measured {angle:+.2f} deg (median of {votes} lines), rotated back"})
    else:
        out.steps.append({"step": "deskew", "applied": False, "detail": "disabled in config"})

    if config.PREPROCESS_SHADOW_REMOVAL:
        k = config.SHADOW_DILATE_KERNEL
        background = cv2.medianBlur(cv2.dilate(gray, np.ones((k, k), np.uint8)), config.SHADOW_MEDIAN_BLUR)
        diff = 255 - cv2.absdiff(gray, background)
        gray = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX)
        out.steps.append({"step": "shadow removal", "applied": True,
                          "detail": f"{k}x{k} dilate, median {config.SHADOW_MEDIAN_BLUR}, absdiff, normalise"})
    else:
        out.steps.append({"step": "shadow removal", "applied": False, "detail": "disabled in config"})

    sigma = estimate_noise(gray)
    if config.PREPROCESS_DENOISE and sigma > config.DENOISE_NOISE_SIGMA_MIN:
        gray = cv2.fastNlMeansDenoising(gray, None, h=config.DENOISE_STRENGTH_H)
        out.steps.append({"step": "denoise", "applied": True, "detail": f"noise sigma ~{sigma:.1f}"})
    else:
        out.steps.append({"step": "denoise", "applied": False,
                          "detail": f"noise sigma ~{sigma:.1f}, under {config.DENOISE_NOISE_SIGMA_MIN:g}"
                          if config.PREPROCESS_DENOISE else "disabled in config"})

    if config.PREPROCESS_THRESHOLD:
        gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
                                     config.ADAPTIVE_BLOCK_SIZE, config.ADAPTIVE_C)
        out.steps.append({"step": "threshold", "applied": True,
                          "detail": f"Gaussian adaptive, block {config.ADAPTIVE_BLOCK_SIZE}, C {config.ADAPTIVE_C}"})
    else:
        out.steps.append({"step": "threshold", "applied": False, "detail": "disabled in config"})

    if config.PREPROCESS_UPSCALE and w < config.UPSCALE_MIN_WIDTH_PX:
        f = config.UPSCALE_FACTOR
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        out.scale = float(f)
        out.steps.append({"step": "upscale", "applied": True, "detail": f"{f}x (page {w}px wide)"})
    else:
        out.steps.append({"step": "upscale", "applied": False,
                          "detail": f"{w}px wide, at least {config.UPSCALE_MIN_WIDTH_PX}px"
                          if config.PREPROCESS_UPSCALE else "disabled in config"})
    out.image = gray
    return out


def to_png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("could not encode the preprocessed page as PNG")
    return buf.tobytes()


# ---------------------------------------------------------------- OCR

@dataclass
class OcrResult:
    psm: int
    mean_conf: float                    # 0-100, length-weighted, words with conf > 0
    legibility: float                   # mean_conf / 100
    text: str
    words: list[dict[str, Any]]         # text, conf, box in processed-image pixels
    tried: list[dict[str, Any]]         # every PSM attempted and its score


def _read(image: np.ndarray, psm: int) -> tuple[float, str, list[dict[str, Any]]]:
    data = pytesseract.image_to_data(image, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    words, lines, weight, total = [], {}, 0, 0.0
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(text)
        x, y, bw, bh = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append({"text": text, "conf": conf, "box": (x, y, x + bw, y + bh)})
        if conf > 0:
            weight += len(text)
            total += conf * len(text)
    mean = total / weight if weight else 0.0
    return mean, "\n".join(" ".join(ws) for ws in lines.values()), words


def ocr_page(image: np.ndarray) -> OcrResult:
    """PSM 6 first; below OCR_RETRY_CONF retry PSM 4 and keep the better reading."""
    _configure_binary()
    best: tuple[float, int, str, list[dict[str, Any]]] | None = None
    tried = []
    for psm in (config.OCR_PSM_PRIMARY, config.OCR_PSM_RETRY):
        mean, text, words = _read(image, psm)
        tried.append({"psm": psm, "mean_conf": round(mean, 1), "words": len(words)})
        if best is None or mean > best[0]:
            best = (mean, psm, text, words)
        if mean >= config.OCR_RETRY_CONF:
            break
    mean, psm, text, words = best
    return OcrResult(psm, mean, round(mean / 100, 3), text, words, tried)
