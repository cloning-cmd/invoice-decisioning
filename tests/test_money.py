"""Decimal arithmetic, printed-amount parsing, and the tolerance band."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.money import (detect_currency, format_money, multiply_minor, parse_amount, po_tolerance_minor,
                                to_minor)


@pytest.mark.parametrize("text, minor", [
    ("1,23,456.78", 12345678),        # Indian lakh grouping
    ("1,234.56", 123456),
    ("₹ 95,250.00", 9525000),
    ("Rs. 1,500/-", 150000),
    ("INR 48,000", 4800000),
    ("6.980,00", 698000),            # European decimal comma
    ("EUR 6.980,00", 698000),
    ("(500.00)", -50000),            # accounting negative
    ("500.00 CR", -50000),
    ("500.00 DR", 50000),
    ("-1,200.50", -120050),
    ("0.5", 50),
])
def test_parse_amount(text, minor):
    assert parse_amount(text) == minor


def test_parse_amount_rejects_non_numbers():
    with pytest.raises(ValueError):
        parse_amount("illegible")


def test_to_minor_refuses_floats():
    with pytest.raises(TypeError):
        to_minor(0.1)


def test_to_minor_rounds_half_up():
    assert to_minor("0.005") == 1 and to_minor(Decimal("2.675")) == 268


def test_multiply_minor():
    assert multiply_minor(60, 75000) == 4500000
    assert multiply_minor("2.5", 33333) == 83333        # 83,332.5 paise rounds half-up to 83,333


@pytest.mark.parametrize("minor, currency, text", [
    (12345678, "INR", "₹1,23,456.78"),
    (9525000, "INR", "₹95,250.00"),
    (698000, "EUR", "€6,980.00"),
    (-50000, "INR", "-₹500.00"),
    (None, "INR", "—"),
])
def test_format_money(minor, currency, text):
    assert format_money(minor, currency) == text


@pytest.mark.parametrize("po_total, tolerance", [
    (200000, 50000),         # Rs 2,000 PO: 2% is Rs 40, floor lifts it to Rs 500
    (2500000, 50000),        # Rs 25,000: 2% = Rs 500 = floor
    (20000000, 400000),      # Rs 2,00,000: 2% = Rs 4,000
    (60000000, 500000),      # Rs 6,00,000: 2% = Rs 12,000, ceiling caps at Rs 5,000
    (500000000, 500000),     # Rs 50 lakh: ceiling
])
def test_po_tolerance_band(po_total, tolerance):
    assert po_tolerance_minor(po_total) == tolerance


@pytest.mark.parametrize("text, expected", [
    ("₹", ("INR", "symbol")), ("INR", ("INR", "iso_code")), ("Rs.", ("INR", "iso_code")),
    ("Grand Total (INR)", ("INR", "iso_code")), ("€ 6.980,00", ("EUR", "symbol")), ("", (None, None)),
])
def test_detect_currency(text, expected):
    assert detect_currency(text) == expected
