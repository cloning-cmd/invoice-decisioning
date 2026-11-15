"""Decimal-safe currency arithmetic.

Inside the system money is an int of minor units (paise / cents). Decimal is
used only at the boundary, when text becomes a number. Floats never touch money.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app import config

MINOR_PER_MAJOR = 100  # INR and EUR both have 100 minor units; JPY-style currencies are out of scope

CURRENCY_SYMBOLS = {"INR": "₹", "EUR": "€", "USD": "$", "GBP": "£"}


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
