"""Every tunable number in the system lives here, and nowhere else.

If you are asked "why 2%?", this file is the answer. Each constant carries a
one-line comment giving the business reason, not a restatement of the value.
Money is always INTEGER minor units (paise / cents).
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path


# --------------------------------------------------------------------------
# Environment (.env is read here so `python -m app.cli` works without run.sh)
# --------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader. Real environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT_DIR / ".env")

# GOOGLE_API_KEY is accepted too, since that is what Google's own tooling exports.
GEMINI_API_KEY: str | None = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
LLM_CACHE_MODES = ("live", "cached", "offline")
# Default "cached": demo fixtures are instant and free after their first run,
# while an unseen invoice still goes to the live model.
LLM_CACHE_MODE: str = os.environ.get("LLM_CACHE_MODE", "cached").strip().lower()
if LLM_CACHE_MODE not in LLM_CACHE_MODES:
    raise ValueError(f"LLM_CACHE_MODE must be one of {LLM_CACHE_MODES}, got {LLM_CACHE_MODE!r}")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

DATA_DIR = ROOT_DIR / "data"
DB_PATH = Path(os.environ.get("INVOICE_DB_PATH", DATA_DIR / "app.db"))
FIXTURES_DIR = DATA_DIR / "fixtures"
SAMPLES_DIR = DATA_DIR / "samples"
UPLOADS_DIR = DATA_DIR / "uploads"
LLM_CACHE_DIR = DATA_DIR / "llm_cache"
STATIC_DIR = ROOT_DIR / "static"

# Optional explicit tesseract binary (Windows installs are often not on PATH).
TESSERACT_CMD: str | None = os.environ.get("TESSERACT_CMD") or None


def as_of_date() -> date:
    """'Today' for date rules (R09 staleness / future dates). INVOICE_AS_OF_DATE=YYYY-MM-DD pins
    it, so a recorded demo or a test gives the same verdict on any calendar day."""
    pinned = os.environ.get("INVOICE_AS_OF_DATE")
    return date.fromisoformat(pinned) if pinned else date.today()


# --------------------------------------------------------------------------
# The buyer — us. Used by the sample generator and to sanity-check "Bill To".
# --------------------------------------------------------------------------

BUYER = {
    "legal_name": "Halden Precision Engineering Pvt Ltd",
    "gstin": "29AAECH0007L1Z2",
    "state_code": "29",  # Karnataka: same-state vendors charge CGST+SGST, others IGST
    "bill_to": ["Halden Precision Engineering Pvt Ltd", "Plot 14, KIADB Industrial Area",
                "Bommasandra, Bengaluru 560099", "Karnataka, India"],
    "ship_to": ["Halden Precision - Plant 2 (Stores)", "Survey No. 88, Jigani Link Road",
                "Anekal Taluk, Bengaluru 562106", "Karnataka, India"],
}


# --------------------------------------------------------------------------
# S01 Ingest
# --------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB: larger than any real invoice; caps abuse and memory
MAX_PAGES = 15                       # an invoice beyond 15 pages is a statement or a pack, not one invoice
ACCEPTED_MIME = ("application/pdf", "image/png", "image/jpeg", "image/tiff", "image/webp")


# --------------------------------------------------------------------------
# S02 Text layer
# --------------------------------------------------------------------------

# Non-space chars per square inch below which a PDF page is treated as image-only.
# Measured: our one-page digital invoices score ~14 (an A4 page is 96.7 sq.in, so
# 14/sq.in is ~1,400 characters); an image-only scan scores 0, and a scan with a
# scanner-app footer stays under 1. The brief's suggested 40 would class almost
# every real digital invoice as a scan (see ASSUMPTIONS.md).
TEXT_COVERAGE_MIN = 3.0


# --------------------------------------------------------------------------
# S03 OCR + preprocessing (each step toggleable so the demo can show its effect)
# --------------------------------------------------------------------------

RASTER_DPI = 300                    # tesseract's accuracy plateaus around 300 DPI

PREPROCESS_EXIF_ROTATE = True       # phone photos carry orientation in EXIF, not in pixels
PREPROCESS_DESKEW = True
PREPROCESS_SHADOW_REMOVAL = True    # the step that makes phone photographs legible
PREPROCESS_DENOISE = True           # only applied when estimated noise is high (below)
PREPROCESS_THRESHOLD = True
PREPROCESS_UPSCALE = True

DESKEW_CANNY_LOW = 50               # Canny hysteresis thresholds for edge detection
DESKEW_CANNY_HIGH = 150
DESKEW_HOUGH_THRESHOLD = 100        # min votes for a line; filters out text-noise segments
DESKEW_HOUGH_MIN_LINE_FRAC = 0.25   # a line must span 25% of page width to count as a rule/row
DESKEW_HOUGH_MAX_GAP_PX = 20        # bridge small breaks in table borders
DESKEW_NEAR_HORIZONTAL_DEG = 20.0   # only lines within ±20° of horizontal vote on the skew
DESKEW_MIN_ANGLE_DEG = 0.3          # below this, rotating costs more sharpness than it gains
DESKEW_MAX_ANGLE_DEG = 15.0         # beyond this it is a photo problem, not skew: flag, don't rotate

SHADOW_DILATE_KERNEL = 7            # 7x7 dilation erases text strokes, leaving the background lighting
SHADOW_MEDIAN_BLUR = 21             # smooths the background estimate so it does not carry text edges

DENOISE_NOISE_SIGMA_MIN = 8.0       # estimated noise sigma above which fastNlMeans is worth its cost
DENOISE_STRENGTH_H = 10             # fastNlMeans filter strength; higher erodes thin glyphs

ADAPTIVE_BLOCK_SIZE = 31            # neighbourhood for Gaussian adaptive threshold (must be odd)
ADAPTIVE_C = 11                     # constant subtracted from the local mean

UPSCALE_MIN_WIDTH_PX = 1500         # below this, glyphs are too small for tesseract's LSTM
UPSCALE_FACTOR = 2

OCR_PSM_PRIMARY = 6                 # uniform block of text: best on most single-column invoices
OCR_PSM_RETRY = 4                   # single column of variable sizes: better on tabular layouts
OCR_RETRY_CONF = 55.0               # mean word conf (0-100) below which we try the other PSM
OCR_LEGIBILITY_MIN = 0.55           # page legibility (0-1) below which the page is marked degraded


# --------------------------------------------------------------------------
# S04 Extraction (LLM)
# --------------------------------------------------------------------------

# Overridable from .env so a model change needs no code edit (cache entries record the model used).
# Measured 2026-11-16 on the happy path: gemini-2.5-flash is closed to new keys
# (404); gemini-3.8-flash took 212 s, then 504'd even on a one-word prompt;
# gemini-3.5-flash read the same values in 8 s. Change here or via .env.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash")
LLM_TEMPERATURE = 0.0               # extraction should be reproducible, not creative
LLM_MAX_TOKENS = 8000               # a 40-line invoice with evidence strings fits comfortably
LLM_IMAGE_MAX_SIDE_PX = 1568        # the model's native resolution; larger only costs tokens
LLM_RASTER_DPI = 150                # A4 at 150 DPI is 1754px tall: just above 1568, so no detail is lost
LLM_IMAGE_JPEG_QUALITY = 85         # keeps small print legible while keeping the payload small
LLM_SCHEMA_RETRIES = 1              # one retry with the validator error; a second failure is real
# Extraction is reading, not reasoning. MINIMAL was no faster than LOW and read
# the same values; LOW keeps a little headroom for messy scans.
LLM_THINKING_LEVEL = os.environ.get("LLM_THINKING_LEVEL", "LOW")
# A hung API call must fail the stage (HOLD, readable reason), never hang the run.
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "180"))


# --------------------------------------------------------------------------
# S05 Normalise
# --------------------------------------------------------------------------

# Day/month order by vendor default currency. The vendor master has no country
# column, and currency is the closest proxy we hold (see ASSUMPTIONS.md).
DATE_ORDER_BY_CURRENCY = {"INR": "DMY", "EUR": "DMY", "GBP": "DMY", "USD": "MDY"}

# Stripped when canonicalising vendor names, longest first so "PVT LTD" wins over "LTD".
VENDOR_LEGAL_SUFFIXES = (
    "PRIVATE LIMITED", "PVT LTD", "PVT", "LIMITED", "LTD", "LLP", "INC", "GMBH",
    "CO", "COMPANY", "CORP", "CORPORATION",
)


# --------------------------------------------------------------------------
# S06 Self-check
# --------------------------------------------------------------------------

ROUNDING_TOLERANCE_MINOR = 200      # Rs 2.00: absorbs per-line rounding and "Round off" lines


# --------------------------------------------------------------------------
# S07 Match
# --------------------------------------------------------------------------

VENDOR_MATCH_MIN = 88               # rapidfuzz score at/above which a vendor name match is trusted
VENDOR_MATCH_AMBIGUOUS_MIN = 70     # 70-88: matched, but a human should confirm (R03b)

PO_SCORE_WEIGHT_AMOUNT = 0.50       # amount proximity is the strongest signal of which PO is meant
PO_SCORE_WEIGHT_DATE = 0.25         # invoice inside the PO's validity window
PO_SCORE_WEIGHT_LINES = 0.25        # SKU / description overlap with the PO lines
PO_AMOUNT_SCORE_ZERO_PCT = 0.50     # an invoice 50%+ away from a PO's remaining scores 0 on amount
PO_DATE_SCORE_DECAY_DAYS = 30       # date score falls linearly to 0 this many days outside the window
PO_AUTOMATCH_MIN = 0.80             # below this, no implicit PO match is trusted
PO_MARGIN_MIN = 0.15                # the winner must beat the runner-up clearly, else a human picks
PO_CANDIDATES_SHOWN = 3             # how many candidates to offer a human when we will not auto-match
LINE_DESC_MATCH_MIN = 85            # description similarity at which an invoice line maps to a PO line SKU

# Fuzzy score (0-100) for locating a field's evidence among the page's words.
# Below this we draw no box: a missing box is honest, a wrong box misleads a clerk.
EVIDENCE_MATCH_MIN = 80

# Only these invoice_history statuses consume PO budget; a rejected invoice was never paid.
PO_CONSUMING_STATUSES = ("approved", "paid")


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

# R01: without these five, a payment cannot be posted or audited.
REQUIRED_FIELDS = ("invoice_number", "invoice_date", "supplier.name", "totals.grand_total", "currency")

DUP_DATE_WINDOW_DAYS = 5            # R04b: re-issues typically land within a working week of the original

NON_PO_SPEND_CEILING_MINOR = 25_000_00   # R05b: Rs 25,000 — above this, spend must be pre-authorised by a PO

# R06 / R07 tolerance band:
#   tolerance = min(max(PO_TOLERANCE_PCT * po_amount, PO_TOLERANCE_FLOOR_MINOR),
#                   PO_TOLERANCE_CEIL_MINOR)
# A flat percentage is wrong at both ends. On a Rs 2,000 PO, 2% is Rs 40 and a
# Rs 60 freight rounding sends it to a human who costs more than the variance —
# hence the floor. On a Rs 50 lakh PO, 2% is Rs 1 lakh of unreviewed exposure —
# hence the ceiling.
PO_TOLERANCE_PCT = 0.02
PO_TOLERANCE_FLOOR_MINOR = 500_00        # Rs 500
PO_TOLERANCE_CEIL_MINOR = 5_000_00       # Rs 5,000

STALE_INVOICE_DAYS = 90             # R09: older invoices are usually disputed, duplicated or mis-sent
FUTURE_DATE_GRACE_DAYS = 1          # R09: one day absorbs time zones and vendors dating "tomorrow"

ALLOWED_TAX_RATES = (0, 5, 12, 18, 28)   # R10: the Indian GST slabs, in percent
TAX_RATE_TOLERANCE_PP = 1.0              # R10: ±1 percentage point absorbs rounding on mixed-rate invoices

CONFIDENCE_MIN = 0.85               # R13: below this, a field reading is a guess and must not auto-pay

AUTO_APPROVE_CEILING_MINOR = 1_00_000_00  # Rs 1,00,000: delegated authority limit for unattended payment


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

# Hard failures that mean "do not touch, escalate now" (BLOCK) rather than
# "send back to vendor" (REJECT). A fraud attempt or a double payment must not
# be bounced back to whoever sent it.
BLOCK_RULE_CODES = ("R12_BANK_DETAILS_UNCHANGED", "R04_DUPLICATE_EXACT")

# Reason-code weights: higher = shown first. Ordered by money at risk if wrong.
REASON_WEIGHTS = {
    "BANK_DETAILS_CHANGED": 100,      # whole invoice value lost to a fraudster
    "DUPLICATE_EXACT": 95,            # whole invoice value paid twice
    "VENDOR_NOT_APPROVED": 90,        # paying a blocked/unknown party is a compliance breach
    "PO_VENDOR_MISMATCH": 85,
    "PO_OVER_CONSUMPTION": 80,        # spend above what was authorised
    "CURRENCY_MISMATCH": 75,
    "PO_STATUS_INVALID": 70,
    "PO_REQUIRED_FOR_AMOUNT": 65,
    "REQUIRED_FIELD_MISSING": 60,
    "POSSIBLE_DUPLICATE": 55,
    "PO_AMOUNT_TOLERANCE": 50,
    "GRN_SHORT_RECEIPT": 45,          # paying for goods not yet received
    "GRN_MISSING": 45,
    "ARITHMETIC_MISMATCH": 40,
    "PARTIAL_NOT_ALLOWED": 35,
    "PO_NOT_FOUND": 30,
    "VENDOR_AMBIGUOUS": 28,
    "TAX_IMPLAUSIBLE": 25,
    "DATE_AMBIGUOUS": 22,             # payment date unknown until a human reads the date
    "DATE_INVALID": 20,
    "UNREADABLE_REQUIRED_FIELD": 15,
    "LOW_CONFIDENCE": 10,
    "STAGE_FAILED": 99,               # a pipeline stage could not complete
    "RULE_ERROR": 98,                 # a rule crashed; we fail closed rather than trust a partial check
}

# Approval matrix for APPROVE_PENDING_SIGNOFF: (upper bound inclusive, role).
# None = no upper bound. Bands mirror a typical mid-size delegation of authority.
APPROVAL_MATRIX = (
    (5_00_000_00, "finance_manager"),       # up to Rs 5 lakh
    (25_00_000_00, "financial_controller"), # up to Rs 25 lakh
    (None, "cfo"),
)
FOREIGN_CURRENCY_APPROVER = "financial_controller"  # no FX rates held, so foreign invoices skip the INR bands

# Named owners for recommended next actions (fictional people).
OWNERS = {
    "ap_clerk": "Arjun Desai, AP Clerk",
    "ap_manager": "Meera Krishnan, AP Manager",
    "procurement": "Priya Nair, Procurement",
    "finance_manager": "Rohan Mehta, Finance Manager",
    "financial_controller": "Ananya Rao, Financial Controller",
    "cfo": "Vikram Iyer, CFO",
    "vendor_management": "Sanjay Kulkarni, Vendor Management",
    "stores": "Kavya Shetty, Stores / Receiving",
    "payment_run": "Scheduled payment run (Treasury)",
}

# Roles allowed to override each verdict. BLOCK is locked to the AP manager:
# a clerk must not be able to release a suspected redirection fraud.
OVERRIDE_ROLES = {
    "AUTO_APPROVE": ("ap_clerk", "ap_manager"),
    "APPROVE_PENDING_SIGNOFF": ("ap_clerk", "ap_manager"),
    "HOLD_FOR_REVIEW": ("ap_clerk", "ap_manager"),
    "REJECT": ("ap_clerk", "ap_manager"),
    "BLOCK": ("ap_manager",),
}


# --------------------------------------------------------------------------
# API / live stream
# --------------------------------------------------------------------------

SSE_HEARTBEAT_SECONDS = 15          # proxies drop idle connections at ~30-60s; 15 keeps us safe
RUNS_PAGE_SIZE_DEFAULT = 50
RUNS_PAGE_SIZE_MAX = 200
STATS_SERIES_DAYS = 14              # dashboard sparkline window
# Timestamps are stored in UTC; "today" on the dashboard is the AP team's day
# (IST, UTC+05:30), so a run at 03:00 IST is not counted as yesterday.
REPORTING_UTC_OFFSET_MINUTES = 330
# Actions a person can take on a decided run, and who may take them. Every
# action needs a note: the audit trail must say why, not only what.
ACTION_ACTORS = ("ap_clerk", "ap_manager")
# The document viewer: 150 DPI renders A4 at 1240 px, sharp in a 40%-width
# column and readable when zoomed, without shipping 300 DPI OCR rasters.
VIEWER_RASTER_DPI = 150
VIEWER_JPEG_QUALITY = 85            # photos only; PDF pages are served as PNG (flat colour, crisp text)
