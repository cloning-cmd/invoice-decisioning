"""Vendor resolution, tiered PO matching, cumulative consumption, evidence location."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app import db
from app.models import InvoiceLine, PoRef
from app.pipeline.stages.s05_normalise import find_po_refs, parse_date
from app.pipeline.stages.s07_match import _consumption
from app.services.matching import canonical_vendor_name, locate_evidence, match_po, resolve_vendor


# ---------------------------------------------------------------- vendor

def test_canonical_vendor_name_strips_suffixes_and_punctuation():
    assert canonical_vendor_name("Nordic Comp. Pvt. Ltd.") == "NORDIC COMP"
    assert canonical_vendor_name("Meridian Packaging LLP") == "MERIDIAN PACKAGING"
    assert canonical_vendor_name("Apex Tooling GmbH") == "APEX TOOLING"
    assert canonical_vendor_name("NORDIC COMPONENTS PRIVATE LIMITED") == "NORDIC COMPONENTS"


def test_resolve_vendor_exact(ref):
    assert resolve_vendor("Nordic Components Pvt Ltd", None, ref.vendors).method == "exact"


def test_resolve_vendor_alias_after_ampersand_canonicalisation(ref):
    assert resolve_vendor("Orbit Electricals and Controls", None, ref.vendors).method == "alias"


def test_resolve_vendor_alias(ref):
    vm = resolve_vendor("SHREEJI IND. SUPPLIES", None, ref.vendors)
    assert (vm.vendor_id, vm.method) == ("V002", "alias")


def test_resolve_vendor_tax_id(ref):
    vm = resolve_vendor("KLPL Warehousing", "33AADCK6652Q1Z7", ref.vendors)
    assert (vm.vendor_id, vm.method) == ("V004", "tax_id")


def test_resolve_vendor_fuzzy_confident(ref):
    vm = resolve_vendor("Orbit Electrical Controls Pvt Ltd", None, ref.vendors)
    assert vm.vendor_id == "V007" and vm.method == "fuzzy" and not vm.ambiguous


def test_resolve_vendor_fuzzy_ambiguous(ref):
    vm = resolve_vendor("Meridan Packging Industries", None, ref.vendors)
    assert vm.vendor_id == "V003" and vm.ambiguous and 70 <= vm.score < 88


def test_resolve_vendor_unknown(ref):
    vm = resolve_vendor("Zenith Polymers", None, ref.vendors)
    assert vm.vendor_id is None and vm.method == "none"


# ---------------------------------------------------------------- PO tiers

LINES = [InvoiceLine(1, "Deep groove ball bearing 6204-2RS", "BRG-12", Decimal(60), "NOS", 75000, Decimal(18), 4500000)]


def test_t1_explicit_po_of_this_vendor(ref):
    pm = match_po([PoRef("PO-2041", "PO-2041", True, "extraction")], "V001", ref.pos, {}, 9525000,
                  date(2026, 11, 2), LINES)
    assert (pm.tier, pm.po_number) == ("T1", "PO-2041")


def test_t2_explicit_po_of_other_vendor_is_not_matched(ref):
    pm = match_po([PoRef("PO-2045", "PO-2045", True, "extraction")], "V006", ref.pos, {}, 698000,
                  date(2026, 11, 5), [])
    assert (pm.tier, pm.po_number, pm.quoted_po_number) == ("T2", None, "PO-2045")


def test_t3_implied_match_needs_score_and_margin(ref):
    # Meridian has PO-2043 (Rs 22,500, open) and PO-2049 (Rs 75,000). A Rs 21,900
    # carton invoice with no PO number should land on PO-2043 by amount + lines.
    lines = [InvoiceLine(1, "5-ply corrugated carton 400x300x300", "CTN-5PLY", Decimal(300), "NOS", 5000, None, 1500000)]
    remaining = {"PO-2043": 2250000, "PO-2049": 2072000}
    pm = match_po([], "V003", ref.pos, remaining, 2190000, date(2026, 11, 5), lines)
    assert pm.tier == "T3" and pm.po_number == "PO-2043"
    assert [c["po_number"] for c in pm.candidates][:2] == ["PO-2043", "PO-2049"]


def test_t3_refuses_when_evidence_is_weak_and_offers_candidates(ref):
    # Identical remaining amounts and no line evidence: nothing separates the POs, so a human picks.
    remaining = {"PO-2043": 2190000, "PO-2049": 2190000}
    pm = match_po([], "V003", ref.pos, remaining, 2190000, date(2026, 11, 5), [])
    assert pm.tier is None and pm.po_number is None
    assert len(pm.candidates) == 2 and "a human should choose" in pm.reason


def test_unknown_quoted_po_falls_back_to_search_and_says_so(ref):
    pm = match_po([PoRef("PO-9999", "PO-9999", True, "extraction")], "V001", ref.pos, {}, 9525000,
                  date(2026, 11, 2), LINES)
    assert "PO-9999 not found" in pm.reason


# ---------------------------------------------------------------- consumption (split invoices)

def test_split_invoice_consumption_from_history(seeded_db):
    with db.session(seeded_db) as conn:
        c = _consumption(conn, "PO-2041", 60000000, 19200000)
    assert c.consumed_minor == 45000000 and c.remaining_minor == 15000000
    assert [p["invoice_number"] for p in c.prior_invoices] == ["INV-NC-5501", "INV-NC-5502"]


def test_rejected_history_does_not_consume_po(seeded_db):
    with db.session(seeded_db) as conn:
        c = _consumption(conn, "PO-2031", 2200000, None)        # TF-8840 was rejected
    assert c.consumed_minor == 0


# ---------------------------------------------------------------- normalise helpers

def test_po_refs_found_in_free_text():
    found = {r.value for r in find_po_refs("Supply against our P.O. 2043 dated 12-Oct; ref order 2049", "t", False)}
    assert found == {"PO-2043", "PO-2049"}


def test_po_refs_ignore_po_box():
    assert find_po_refs("PO Box 1234, Bengaluru", "t", False) == []


def test_date_resolved_by_vendor_locale():
    d, readings, warning = parse_date("02/11/2026", "DMY")
    assert d == date(2026, 11, 2) and warning is None and len(readings) == 2


def test_date_ambiguous_without_locale_warns_and_keeps_both():
    d, readings, warning = parse_date("02/11/2026", None)
    assert warning and readings == [date(2026, 11, 2), date(2026, 2, 11)]


def test_date_unambiguous_when_day_over_12():
    assert parse_date("15/03/2026", None) == (date(2026, 3, 15), [date(2026, 3, 15)], None)


def test_date_textual_formats():
    assert parse_date("15 Mar 2026", None)[0] == date(2026, 3, 15)
    assert parse_date("04-NOV-2026", None)[0] == date(2026, 11, 4)
    assert parse_date("2026-11-04", None)[0] == date(2026, 11, 4)


# ---------------------------------------------------------------- evidence -> bbox

WORDS = [{"text": t, "page": 1, "bbox": [0.1 * i, 0.2, 0.1 * i + 0.08, 0.22]}
         for i, t in enumerate(["Invoice", "No.", "INV-NC-5531", "Date", "02/11/2026"])]


def test_locate_evidence_returns_union_box():
    assert locate_evidence("Invoice No. INV-NC-5531", 1, WORDS) == [0.0, 0.2, 0.28, 0.22]


def test_locate_evidence_returns_none_rather_than_a_wrong_box():
    assert locate_evidence("Total Payable 95,250.00", 1, WORDS) is None
