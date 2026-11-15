"""Decimal-safe currency arithmetic.

Inside the system money is an int of minor units (paise / cents). Decimal is
used only at the boundary, when text becomes a number. Floats never touch money.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app import config

MINOR_PER_MAJOR = 100  # INR and EUR both have 100 minor units; JPY-style currencies are out of scope

CURRENCY_SYMBOLS = {"INR": "₹", "EUR": "€", "USD": "$", "GBP": "£"}

# Symbols first, then ISO codes / abbreviations, matching normalise's detection order.
_SYMBOL_TO_ISO = {"₹": "INR", "€": "EUR", "$": "USD", "£": "GBP"}
_CODE_RE = re.compile(r"\b(INR|EUR|USD|GBP|RS\.?|RUPEES?)\b", re.IGNORECASE)
_CODE_TO_ISO = {"INR": "INR", "EUR": "EUR", "USD": "USD", "GBP": "GBP",
                "RS": "INR", "RS.": "INR", "RUPEE": "INR", "RUPEES": "INR"}


def detect_currency(text: str | None) -> tuple[str | None, str | None]:
    """Return (ISO code, how it was found: "symbol" | "iso_code") or (None, None)."""
    if not text:
        return None, None
    for sym, iso in _SYMBOL_TO_ISO.items():
        if sym in text:
            return iso, "symbol"
    m = _CODE_RE.search(text)
    if m:
        return _CODE_TO_ISO[m.group(1).upper()], "iso_code"
    return None, None


def parse_amount(text: str) -> int:
    """Printed amount -> integer minor units, without guessing.

    Handles "₹ 1,23,456.78" (lakh grouping), "1,234.56", "6.980,00" (decimal
    comma), "(500.00)" and "500.00 CR" / "-500" as negatives, trailing "DR".
    Raises ValueError if no number can be read.
    """
    s = text.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    upper = s.upper()
    if re.search(r"\bCR\.?$", upper):
        negative = True
    s = re.sub(r"\b(CR|DR)\.?$", "", s, flags=re.IGNORECASE)
    s = _CODE_RE.sub("", s).strip()
    if s.endswith("/-"):          # Indian convention: "Rs 1,500/-" means exactly 1,500
        s = s[:-2].strip()
    if s.startswith("-") or s.endswith("-"):
        negative = True
    digits = re.sub(r"[^0-9.,]", "", s)
    if not re.search(r"\d", digits):
        raise ValueError(f"no amount in {text!r}")

    last_sep = max(digits.rfind("."), digits.rfind(","))
    if last_sep == -1:
        whole, frac = digits, ""
    else:
        tail = digits[last_sep + 1:]
        # A final separator followed by 1-2 digits is the decimal point; followed
        # by 3 it is a thousands separator ("1,234" or "1.234").
        if 1 <= len(tail) <= 2:
            whole, frac = digits[:last_sep], tail
        else:
            whole, frac = digits, ""
    whole = re.sub(r"[.,]", "", whole) or "0"
    value = Decimal(f"{whole}.{frac or '0'}")
    minor = to_minor(value)
    return -minor if negative else minor


def to_minor(amount: str | int | Decimal) -> int:
    """Convert a major-unit amount ("1234.56", Decimal, int) to integer minor units."""
    if isinstance(amount, float):
        raise TypeError("float is not allowed for money; pass a str or Decimal")
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError(f"not a money amount: {amount!r}") from exc
    return int((value * MINOR_PER_MAJOR).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def multiply_minor(qty: int | float | str | Decimal, unit_minor: int) -> int:
    """qty × unit price, rounded half-up to the minor unit (qty may be fractional, e.g. 2.5 kg)."""
    if isinstance(qty, float):
        qty = str(qty)  # JSON quantities arrive as float; their decimal text is exact enough
    return int((Decimal(str(qty)) * unit_minor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_major(minor: int) -> Decimal:
    return (Decimal(minor) / MINOR_PER_MAJOR).quantize(Decimal("0.01"))


def _group_indian(digits: str) -> str:
    """1234567 -> 12,34,567 (last three, then pairs)."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join(pairs) + "," + tail


def _group_western(digits: str) -> str:
    return f"{int(digits):,}"


def format_money(minor: int | None, currency: str = "INR", symbol: bool = True) -> str:
    """Human display: Indian grouping for INR (₹2,04,800.00), western otherwise."""
    if minor is None:
        return "—"
    sign = "-" if minor < 0 else ""
    major = to_major(abs(minor))
    whole, _, frac = f"{major:.2f}".partition(".")
    grouped = _group_indian(whole) if currency == "INR" else _group_western(whole)
    prefix = CURRENCY_SYMBOLS.get(currency, currency + " ") if symbol else ""
    return f"{sign}{prefix}{grouped}.{frac}"


def po_tolerance_minor(po_amount_minor: int) -> int:
    """The tolerance band for PO amount comparisons (R06, R07).

    Percentage of the PO, but never below the floor (variance not worth a
    human's time) and never above the ceiling (exposure too large to wave
    through). See config.PO_TOLERANCE_* for the reasoning.
    """
    pct_band = (Decimal(po_amount_minor) * Decimal(str(config.PO_TOLERANCE_PCT))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP)
    return min(max(int(pct_band), config.PO_TOLERANCE_FLOOR_MINOR), config.PO_TOLERANCE_CEIL_MINOR)
