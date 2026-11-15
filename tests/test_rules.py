"""Every rule, both branches, on a hand-built context. Plus the decision matrix.

Expectations are literal numbers on purpose: changing a threshold in
config.py should break a test here, so the change is deliberate.
"""
from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal

from app.models import (Consumption, DuplicateCheck, GrnCheck, POMatch, RunContext, VendorMatch, to_jsonable)
from app.pipeline.stages import s06_selfcheck, s09_decide
from app.rules import definitions  # noqa: F401 — registers the rules
from app.rules.engine import REGISTRY, run_all, run_one

from tests.conftest import make_invoice


def check(ctx: RunContext, code: str):
    return run_one(next(s for s in REGISTRY if s.code == code), ctx)


def decide(ctx: RunContext):
    ctx.rule_results = run_all(ctx)
    return s09_decide.decide(ctx)


# ---------------------------------------------------------------- engine

def test_registry_has_all_20_rules_with_unique_codes():
    codes = [s.code for s in REGISTRY]
    assert len(codes) == 20 and len(set(codes)) == 20


def test_happy_context_passes_everything(happy_ctx):
    results = run_all(happy_ctx)
    assert [r.code for r in results if r.status in ("fail", "warn")] == []


def _snapshot(ctx: RunContext) -> str:
    skip = {"ref", "file_bytes", "page_images"}
    return json.dumps(to_jsonable({k: v for k, v in vars(ctx).items() if k not in skip}), sort_keys=True)


def test_rules_do_not_mutate_ctx(happy_ctx):
    before = _snapshot(happy_ctx)
    run_all(happy_ctx)
    assert _snapshot(happy_ctx) == before


def test_a_crashing_rule_fails_closed_and_others_still_run(happy_ctx):
    happy_ctx.consumption = "not a Consumption"  # forces R06/R07 to raise
    results = run_all(happy_ctx)
    assert len(results) == 20
    crashed = [r for r in results if "RULE_ERROR" in r.reason_codes]
    assert crashed and all(r.status == "fail" for r in crashed)


# ---------------------------------------------------------------- R01

def test_r01_pass(happy_ctx):
    assert check(happy_ctx, "R01_REQUIRED_FIELDS").status == "pass"


def test_r01_fail_missing_invoice_number(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_number=None)
    r = check(happy_ctx, "R01_REQUIRED_FIELDS")
    assert r.status == "fail" and r.severity == "hard"
    assert r.reason_codes == ["REQUIRED_FIELD_MISSING:invoice_number"]


# ---------------------------------------------------------------- R02

def test_r02_pass(happy_ctx):
    assert check(happy_ctx, "R02_ARITHMETIC_INTEGRITY").status == "pass"


def test_r02_fail_line_total_off_flags_document_discrepancy(happy_ctx):
    inv = make_invoice()
    inv.lines[0].line_total_minor = 4600000          # 60 × 750 is 45,000, not 46,000
    happy_ctx.invoice = inv
    happy_ctx.selfcheck = s06_selfcheck.check(happy_ctx)
    r = check(happy_ctx, "R02_ARITHMETIC_INTEGRITY")
    assert r.status == "fail" and r.reason_codes == ["ARITHMETIC_MISMATCH"]
    assert "document discrepancy" in r.message


def test_r02_low_confidence_field_is_flagged_as_possible_misread(happy_ctx):
    inv = make_invoice()
    inv.grand_total_minor = 9825000
    inv.confidence["totals.grand_total"] = 0.6
    happy_ctx.invoice = inv
    happy_ctx.selfcheck = s06_selfcheck.check(happy_ctx)
    r = check(happy_ctx, "R02_ARITHMETIC_INTEGRITY")
    assert r.status == "fail" and "possible misread" in r.message


def test_r02_rounding_within_two_rupees_passes(happy_ctx):
    happy_ctx.invoice = make_invoice(grand_total_minor=9525000 + 199)
    happy_ctx.selfcheck = s06_selfcheck.check(happy_ctx)
    assert check(happy_ctx, "R02_ARITHMETIC_INTEGRITY").status == "pass"


# ---------------------------------------------------------------- R03 / R03b

def test_r03_pass(happy_ctx):
    assert check(happy_ctx, "R03_VENDOR_APPROVED").status == "pass"


def test_r03_fail_unknown_vendor(happy_ctx):
    happy_ctx.vendor_match = VendorMatch(None, "none", 41.0, False, [])
    assert check(happy_ctx, "R03_VENDOR_APPROVED").reason_codes == ["VENDOR_NOT_APPROVED"]


def test_r03_fail_on_hold_vendor(happy_ctx):
    happy_ctx.vendor_match = VendorMatch("V005", "exact", 100.0, False, [])
    r = check(happy_ctx, "R03_VENDOR_APPROVED")
    assert r.status == "fail" and "on_hold" in r.message


def test_r03_fail_blocked_vendor(happy_ctx):
    happy_ctx.vendor_match = VendorMatch("V008", "exact", 100.0, False, [])
    assert check(happy_ctx, "R03_VENDOR_APPROVED").status == "fail"


def test_r03b_pass(happy_ctx):
    assert check(happy_ctx, "R03b_VENDOR_AMBIGUOUS").status == "pass"


def test_r03b_fail_ambiguous(happy_ctx):
    happy_ctx.vendor_match = VendorMatch("V001", "fuzzy", 79.0, True, [])
    r = check(happy_ctx, "R03b_VENDOR_AMBIGUOUS")
    assert r.status == "fail" and r.severity == "soft" and "79" in r.message


# ---------------------------------------------------------------- R04 / R04b

def test_r04_pass(happy_ctx):
    assert check(happy_ctx, "R04_DUPLICATE_EXACT").status == "pass"


def test_r04_fail_same_vendor_and_number(happy_ctx):
    happy_ctx.duplicates = DuplicateCheck([{"kind": "same_vendor_and_number", "invoice_number": "INV-NC-5531",
                                            "invoice_date": "2026-11-02", "status": "paid",
                                            "total_minor": 9525000}], [])
    r = check(happy_ctx, "R04_DUPLICATE_EXACT")
    assert r.status == "fail" and r.severity == "hard" and r.reason_codes == ["DUPLICATE_EXACT"]


def test_r04b_pass(happy_ctx):
    assert check(happy_ctx, "R04b_DUPLICATE_NEAR").status == "pass"


def test_r04b_fail_reissue(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_number="INV-SH-7793", invoice_date=date(2026, 11, 4))
    happy_ctx.duplicates = DuplicateCheck([], [{"invoice_number": "INV-SH-7781", "invoice_date": "2026-11-02",
                                                "total_minor": 4620000, "status": "paid"}])
    r = check(happy_ctx, "R04b_DUPLICATE_NEAR")
    assert r.status == "fail" and r.severity == "soft"
    assert r.reason_codes == ["POSSIBLE_DUPLICATE"] and "2 days apart" in r.message


# ---------------------------------------------------------------- R05 family

def test_r05_pass(happy_ctx):
    assert check(happy_ctx, "R05_PO_RESOLVED").status == "pass"


def test_r05_fail_no_po(happy_ctx):
    happy_ctx.po_match = POMatch(None, None, None, [], "no confident implied match")
    assert check(happy_ctx, "R05_PO_RESOLVED").reason_codes == ["PO_NOT_FOUND"]


def test_r05_not_applicable_when_foreign_po_quoted(happy_ctx):
    happy_ctx.po_match = POMatch("T2", None, "PO-2042", [], "other vendor")
    assert check(happy_ctx, "R05_PO_RESOLVED").status == "not_applicable"


def test_r05b_pass_small_non_po_spend(happy_ctx):
    happy_ctx.po_match = POMatch(None, None, None, [], "none")
    happy_ctx.invoice = make_invoice(grand_total_minor=2500000)        # exactly Rs 25,000 is allowed
    assert check(happy_ctx, "R05b_PO_REQUIRED_FOR_AMOUNT").status == "pass"


def test_r05b_fail_above_ceiling(happy_ctx):
    happy_ctx.po_match = POMatch(None, None, None, [], "none")
    happy_ctx.invoice = make_invoice(grand_total_minor=2500001)
    r = check(happy_ctx, "R05b_PO_REQUIRED_FOR_AMOUNT")
    assert r.status == "fail" and r.severity == "hard"


def test_r05c_pass(happy_ctx):
    assert check(happy_ctx, "R05c_PO_VENDOR_MISMATCH").status == "pass"


def test_r05c_fail_po_of_other_vendor(happy_ctx):
    happy_ctx.po_match = POMatch("T2", None, "PO-2045", [], "other vendor")
    happy_ctx.vendor_match = VendorMatch("V006", "exact", 100.0, False, [])
    r = check(happy_ctx, "R05c_PO_VENDOR_MISMATCH")
    assert r.status == "fail" and r.reason_codes == ["PO_VENDOR_MISMATCH"] and "V001" in r.message


def test_r05d_pass(happy_ctx):
    assert check(happy_ctx, "R05d_PO_STATUS_INVALID").status == "pass"


def test_r05d_fail_closed_po_even_when_quoted_by_wrong_vendor(happy_ctx):
    happy_ctx.po_match = POMatch("T2", None, "PO-2045", [], "other vendor")
    r = check(happy_ctx, "R05d_PO_STATUS_INVALID")
    assert r.status == "fail" and "closed" in r.message


def test_r05d_fail_expired_po(happy_ctx):
    happy_ctx.po_match = POMatch("T1", "PO-2048", "PO-2048", [], "quoted")
    r = check(happy_ctx, "R05d_PO_STATUS_INVALID")
    assert r.status == "fail" and "expired on 2026-09-30" in r.message


# ---------------------------------------------------------------- R06

def _single_po_ctx(ctx, po_number, po_total, invoice_total):
    ctx.po_match = POMatch("T1", po_number, po_number, [], "quoted")
    ctx.consumption = Consumption(po_number, po_total, 0, po_total, [], invoice_total)
    ctx.invoice = make_invoice(grand_total_minor=invoice_total)
    return ctx


def test_r06_pass_partial_invoice_under_remaining(happy_ctx):
    assert check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE").status == "pass"


def test_r06_fail_edge5_variance_2_4_pct(happy_ctx):
    _single_po_ctx(happy_ctx, "PO-2047", 20000000, 20480000)   # Rs 2,04,800 vs Rs 2,00,000
    r = check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE")
    assert r.status == "fail" and r.reason_codes == ["PO_AMOUNT_TOLERANCE"]
    assert r.evidence["tolerance_minor"] == 400000             # 2% of 2,00,000 = Rs 4,000
    assert "2.4%" in r.message and "₹4,800.00" in r.message


def test_r06_pass_at_exact_tolerance(happy_ctx):
    _single_po_ctx(happy_ctx, "PO-2047", 20000000, 20400000)
    assert check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE").status == "pass"


def test_r06_floor_applies_on_small_po(happy_ctx):
    # PO-2043 is Rs 22,500: 2% is Rs 450, so the Rs 500 floor sets the band.
    _single_po_ctx(happy_ctx, "PO-2043", 2250000, 2300000)     # Rs 500 over: inside the floor
    r = check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE")
    assert r.status == "pass" and r.evidence["tolerance_minor"] == 50000
    _single_po_ctx(happy_ctx, "PO-2043", 2250000, 2300100)     # Rs 501 over: outside
    assert check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE").status == "fail"


def test_r06_fail_under_billing_on_non_partial_po(happy_ctx):
    _single_po_ctx(happy_ctx, "PO-2047", 20000000, 19000000)
    assert check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE").status == "fail"


def test_r06_fail_over_remaining_on_partial_po(happy_ctx):
    happy_ctx.invoice = make_invoice(grand_total_minor=15600000)  # Rs 6,000 over the 1,50,000 remaining; tol 5,000
    assert check(happy_ctx, "R06_PO_AMOUNT_TOLERANCE").status == "fail"


# ---------------------------------------------------------------- R07 / R07b

def test_r07_pass_cumulative_within_po(happy_ctx):
    r = check(happy_ctx, "R07_PO_OVER_CONSUMPTION")
    assert r.status == "pass" and r.evidence["cumulative_minor"] == 54525000


def test_r07_fail_edge2_third_tranche(happy_ctx):
    happy_ctx.invoice = make_invoice(grand_total_minor=19200000)  # Rs 1,92,000
    r = check(happy_ctx, "R07_PO_OVER_CONSUMPTION")
    assert r.status == "fail" and r.severity == "hard" and r.reason_codes == ["PO_OVER_CONSUMPTION"]
    assert r.evidence["overage_minor"] == 4200000                  # Rs 42,000 over Rs 6,00,000
    assert "INV-NC-5501" in r.message and "INV-NC-5502" in r.message and "₹42,000.00 over" in r.message


def test_r07_not_applicable_without_prior_invoices(happy_ctx):
    _single_po_ctx(happy_ctx, "PO-2047", 20000000, 20480000)
    assert check(happy_ctx, "R07_PO_OVER_CONSUMPTION").status == "not_applicable"


def test_r07b_pass_when_partial_allowed(happy_ctx):
    assert check(happy_ctx, "R07b_PARTIAL_NOT_ALLOWED").status == "pass"


def test_r07b_fail_partial_on_non_partial_po(happy_ctx):
    _single_po_ctx(happy_ctx, "PO-2042", 18400000, 9000000)
    r = check(happy_ctx, "R07b_PARTIAL_NOT_ALLOWED")
    assert r.status == "fail" and r.reason_codes == ["PARTIAL_NOT_ALLOWED"]


# ---------------------------------------------------------------- R08

def test_r08_pass(happy_ctx):
    assert check(happy_ctx, "R08_GRN_COVERAGE").status == "pass"


def test_r08_fail_short_receipt(happy_ctx):
    happy_ctx.grn = GrnCheck("PO-2047", ["GRN-7144"],
                             [{"sku": "DB-8W", "invoiced": "100", "received": "80", "short_by": "20"}], [])
    r = check(happy_ctx, "R08_GRN_COVERAGE")
    assert r.status == "fail" and r.reason_codes == ["GRN_SHORT_RECEIPT"]
    assert "DB-8W invoiced 100, received 80" in r.message


def test_r08_fail_no_receipt(happy_ctx):
    happy_ctx.grn = GrnCheck("PO-2050", [], [], [])
    assert check(happy_ctx, "R08_GRN_COVERAGE").reason_codes == ["GRN_MISSING"]


def test_r08_warn_unmapped_line(happy_ctx):
    g = copy.deepcopy(happy_ctx.grn)
    g.unmatched_lines = [{"line_no": 4, "description": "Packing charges", "sku": None, "best_score": 31.0}]
    happy_ctx.grn = g
    assert check(happy_ctx, "R08_GRN_COVERAGE").status == "warn"


# ---------------------------------------------------------------- R09

def test_r09_pass(happy_ctx):
    assert check(happy_ctx, "R09_DATE_SANITY").status == "pass"


def test_r09_fail_before_po_issue(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_date=date(2026, 8, 1))      # PO-2041 issued 2026-08-18
    assert "before PO-2041 was issued" in check(happy_ctx, "R09_DATE_SANITY").message


def test_r09_fail_future_date(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_date=date(2026, 11, 18))    # today 16th + 1 day grace
    assert check(happy_ctx, "R09_DATE_SANITY").status == "fail"


def test_r09_pass_tomorrow_within_grace(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_date=date(2026, 11, 17))
    assert check(happy_ctx, "R09_DATE_SANITY").status == "pass"


def test_r09_fail_stale(happy_ctx):
    happy_ctx.po_match = POMatch(None, None, None, [], "none")
    happy_ctx.invoice = make_invoice(invoice_date=date(2026, 8, 17))    # 91 days before 2026-11-16
    assert "91 days old" in check(happy_ctx, "R09_DATE_SANITY").message


def test_r09_fail_ambiguous(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_date_alternatives=[date(2026, 11, 2), date(2026, 2, 11)])
    assert check(happy_ctx, "R09_DATE_SANITY").reason_codes == ["DATE_AMBIGUOUS"]


# ---------------------------------------------------------------- R10

def test_r10_pass(happy_ctx):
    assert check(happy_ctx, "R10_TAX_PLAUSIBILITY").status == "pass"


def test_r10_fail_rate_not_a_slab(happy_ctx):
    inv = make_invoice(effective_tax_rate_pct=Decimal("15.50"))
    for l in inv.lines:
        l.tax_rate_pct = None
    happy_ctx.invoice = inv
    assert check(happy_ctx, "R10_TAX_PLAUSIBILITY").reason_codes == ["TAX_IMPLAUSIBLE"]


def test_r10_pass_within_one_point_of_slab(happy_ctx):
    inv = make_invoice(effective_tax_rate_pct=Decimal("17.10"))
    for l in inv.lines:
        l.tax_rate_pct = None
    happy_ctx.invoice = inv
    assert check(happy_ctx, "R10_TAX_PLAUSIBILITY").status == "pass"


def test_r10_fail_breakdown_does_not_sum(happy_ctx):
    happy_ctx.invoice = make_invoice(tax_total_minor=1500000)       # breakdown sums to 14,400
    assert check(happy_ctx, "R10_TAX_PLAUSIBILITY").status == "fail"


# ---------------------------------------------------------------- R11

def test_r11_pass(happy_ctx):
    assert check(happy_ctx, "R11_CURRENCY_MATCH").status == "pass"


def test_r11_fail_eur_invoice_on_inr_po(happy_ctx):
    happy_ctx.invoice = make_invoice(currency="EUR")
    r = check(happy_ctx, "R11_CURRENCY_MATCH")
    assert r.status == "fail" and r.severity == "hard" and r.reason_codes == ["CURRENCY_MISMATCH"]


# ---------------------------------------------------------------- R12

def test_r12_pass(happy_ctx):
    assert check(happy_ctx, "R12_BANK_DETAILS_UNCHANGED").status == "pass"


def test_r12_pass_when_no_bank_details_printed(happy_ctx):
    happy_ctx.invoice = make_invoice(bank_account_last4=None, bank_ifsc=None)
    assert check(happy_ctx, "R12_BANK_DETAILS_UNCHANGED").status == "pass"


def test_r12_fail_edge4_account_and_ifsc_changed(happy_ctx):
    happy_ctx.vendor_match = VendorMatch("V004", "exact", 100.0, False, [])
    happy_ctx.invoice = make_invoice(bank_account_last4="9043", bank_ifsc="ICIC0003921")
    r = check(happy_ctx, "R12_BANK_DETAILS_UNCHANGED")
    assert r.status == "fail" and r.severity == "hard" and r.reason_codes == ["BANK_DETAILS_CHANGED"]
    assert "9043" in r.message and "6652" in r.message


def test_r12_fail_ifsc_alone_changed(happy_ctx):
    happy_ctx.invoice = make_invoice(bank_ifsc="ICIC0000001")
    assert check(happy_ctx, "R12_BANK_DETAILS_UNCHANGED").status == "fail"


# ---------------------------------------------------------------- R13 / R14

def test_r13_pass(happy_ctx):
    assert check(happy_ctx, "R13_EXTRACTION_CONFIDENCE").status == "pass"


def test_r13_fail_low_confidence(happy_ctx):
    inv = make_invoice()
    inv.confidence["invoice_date"] = 0.84
    happy_ctx.invoice = inv
    r = check(happy_ctx, "R13_EXTRACTION_CONFIDENCE")
    assert r.status == "fail" and "invoice_date read at 0.84" in r.message


def test_r13_fail_illegible_page(happy_ctx):
    happy_ctx.legibility_by_page = {1: 0.5}
    assert check(happy_ctx, "R13_EXTRACTION_CONFIDENCE").reason_codes == ["LOW_CONFIDENCE"]


def test_r13_clerk_supplied_value_counts_as_certain(happy_ctx):
    inv = make_invoice(human_supplied=["invoice_number"])
    inv.confidence["invoice_number"] = 0.0
    happy_ctx.invoice = inv
    assert check(happy_ctx, "R13_EXTRACTION_CONFIDENCE").status == "pass"


def test_r14_pass(happy_ctx):
    assert check(happy_ctx, "R14_UNREADABLE_REGIONS").status == "pass"


def test_r14_fail_region_hides_required_field(happy_ctx):
    happy_ctx.invoice = make_invoice(invoice_number=None)
    happy_ctx.extraction = {**happy_ctx.extraction,
                            "unreadable_regions": [{"page": 1, "description": "invoice number under shadow"}]}
    r = check(happy_ctx, "R14_UNREADABLE_REGIONS")
    assert r.status == "fail" and r.evidence["affected"] == ["invoice_number"]


def test_r14_warn_region_but_fields_read(happy_ctx):
    happy_ctx.extraction = {**happy_ctx.extraction,
                            "unreadable_regions": [{"page": 1, "description": "stamp over the footer"}]}
    assert check(happy_ctx, "R14_UNREADABLE_REGIONS").status == "warn"


# ---------------------------------------------------------------- decision matrix

def test_matrix_clean_under_ceiling_auto_approves_with_payment_date(happy_ctx):
    d = decide(happy_ctx)
    assert d.verdict == "AUTO_APPROVE" and d.reason_codes == []
    assert d.payment_date == date(2026, 12, 2)                          # 2026-11-02 + 30 days


def test_matrix_clean_above_one_lakh_needs_signoff(happy_ctx):
    happy_ctx.invoice = make_invoice(grand_total_minor=14850000, shipping_minor=5410000)
    happy_ctx.selfcheck = s06_selfcheck.check(happy_ctx)
    d = decide(happy_ctx)
    assert d.verdict == "APPROVE_PENDING_SIGNOFF" and "Finance Manager" in d.owner


def test_matrix_soft_failure_holds_and_orders_by_weight(happy_ctx):
    _single_po_ctx(happy_ctx, "PO-2047", 20000000, 20480000)
    happy_ctx.vendor_match = VendorMatch("V007", "exact", 100.0, False, [])
    happy_ctx.invoice = make_invoice(grand_total_minor=20480000, bank_account_last4="3317",
                                     bank_ifsc="KKBK0000172", subtotal_minor=None,
                                     invoice_date=date(2026, 11, 3))
    happy_ctx.grn = GrnCheck("PO-2047", ["GRN-7144"],
                             [{"sku": "DB-8W", "invoiced": "100", "received": "80", "short_by": "20"}], [])
    happy_ctx.selfcheck = {"checks": [], "discrepancies": [], "derived": {}}
    d = decide(happy_ctx)
    assert d.verdict == "HOLD_FOR_REVIEW"
    assert d.reason_codes == ["PO_AMOUNT_TOLERANCE", "GRN_SHORT_RECEIPT"]


def test_matrix_hard_failure_rejects_with_only_hard_reasons(happy_ctx):
    happy_ctx.invoice = make_invoice(grand_total_minor=19200000, shipping_minor=None, subtotal_minor=None)
    happy_ctx.selfcheck = {"checks": [], "discrepancies": [], "derived": {}}
    d = decide(happy_ctx)
    assert d.verdict == "REJECT" and d.reason_codes == ["PO_OVER_CONSUMPTION"]


def test_matrix_bank_change_blocks_not_rejects(happy_ctx):
    happy_ctx.invoice = make_invoice(bank_account_last4="9043")
    d = decide(happy_ctx)
    assert d.verdict == "BLOCK" and d.reason_codes == ["BANK_DETAILS_CHANGED"]
    assert "do not reply to the invoice email" in d.next_action


def test_matrix_exact_duplicate_blocks(happy_ctx):
    happy_ctx.duplicates = DuplicateCheck([{"kind": "same_file_in_history", "invoice_number": "X",
                                            "status": "paid"}], [])
    assert decide(happy_ctx).verdict == "BLOCK"
