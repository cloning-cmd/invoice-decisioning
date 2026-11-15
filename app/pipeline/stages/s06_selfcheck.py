"""S06 Self-check: does the document add up, as read, before any PO comparison?

A mismatch is either a genuine vendor error or an OCR/vision misread. We say
which is more likely by checking whether an involved field was read with low
confidence — a human handles the two very differently.
"""
from __future__ import annotations

from typing import Any

from app import config
from app.models import RunContext, StageResult
from app.services.money import format_money, multiply_minor


def _suspect(inv_conf: dict[str, float], paths: list[str]) -> str:
    low = [p for p in paths if inv_conf.get(p, 1.0) < config.CONFIDENCE_MIN]
    return f"possible misread ({', '.join(low)} read with low confidence)" if low else "document discrepancy"


def check(ctx: RunContext) -> dict[str, Any]:
    inv = ctx.invoice
    cur = inv.currency or "INR"
    tol = config.ROUNDING_TOLERANCE_MINOR
    checks: list[dict[str, Any]] = []
    derived: dict[str, int] = {}

    def add(name: str, expected: int, stated: int, paths: list[str]) -> None:
        delta = stated - expected
        ok = abs(delta) <= tol
        checks.append({"check": name, "expected_minor": expected, "stated_minor": stated, "delta_minor": delta,
                       "ok": ok, "fields": paths, "suspect": None if ok else _suspect(inv.confidence, paths),
                       "message": f"{name}: stated {format_money(stated, cur)}, computed {format_money(expected, cur)}"
                                  + ("" if ok else f", off by {format_money(abs(delta), cur)}")})

    for i, line in enumerate(inv.lines):
        if line.qty is not None and line.unit_price_minor is not None and line.line_total_minor is not None:
            add(f"line {line.line_no} qty × rate", multiply_minor(line.qty, line.unit_price_minor),
                line.line_total_minor,
                [f"line_items[{i}].qty", f"line_items[{i}].unit_price", f"line_items[{i}].line_total"])

    line_totals = [l.line_total_minor for l in inv.lines]
    lines_sum = sum(line_totals) if line_totals and all(t is not None for t in line_totals) else None
    subtotal = inv.subtotal_minor
    if subtotal is not None and lines_sum is not None:
        add("sum of lines vs subtotal", lines_sum, subtotal,
            ["totals.subtotal"] + [f"line_items[{i}].line_total" for i in range(len(inv.lines))])
    elif subtotal is None and lines_sum is not None:
        subtotal = derived["subtotal_minor"] = lines_sum     # derived, never written over a stated value

    tax = inv.tax_total_minor
    parts = [b.amount_minor for b in inv.tax_breakdown]
    parts_sum = sum(parts) if parts and all(p is not None for p in parts) else None
    if tax is not None and parts_sum is not None:
        add("tax breakdown vs tax total", parts_sum, tax,
            ["totals.tax_total"] + [f"totals.tax_breakdown[{i}].amount" for i in range(len(parts))])
    elif tax is None and parts_sum is not None:
        tax = derived["tax_total_minor"] = parts_sum

    if inv.grand_total_minor is not None and subtotal is not None and tax is not None:
        expected = subtotal + tax - (inv.discount_minor or 0) + (inv.shipping_minor or 0)
        add("subtotal + tax − discount + shipping vs grand total", expected, inv.grand_total_minor,
            ["totals.grand_total", "totals.subtotal", "totals.tax_total"])

    return {"checks": checks, "derived": derived, "tolerance_minor": tol,
            "discrepancies": [c for c in checks if not c["ok"]]}


def run(ctx: RunContext) -> StageResult:
    if ctx.invoice is None:
        return StageResult("failed", "no normalised invoice")
    result = check(ctx)
    ctx.selfcheck = result
    n, bad = len(result["checks"]), result["discrepancies"]
    derived = f"; derived {', '.join(result['derived'])}" if result["derived"] else ""
    if n == 0:
        return StageResult("warn", "not enough figures printed to cross-check the arithmetic", result)
    if bad:
        return StageResult("warn", f"{len(bad)} of {n} checks off by more than "
                                   f"{format_money(config.ROUNDING_TOLERANCE_MINOR)}: {bad[0]['message']}{derived}",
                           result)
    return StageResult("ok", f"all {n} arithmetic checks agree within "
                             f"{format_money(config.ROUNDING_TOLERANCE_MINOR)}{derived}", result)
