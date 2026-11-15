"""Shared fixtures: a seeded throwaway database, reference data, and a hand-built
'happy path' context that each rule test perturbs in exactly one place."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import config
from app.models import (Consumption, DuplicateCheck, GrnCheck, InvoiceLine, NormalisedInvoice, POMatch, PoRef,
                        RunContext, TaxComponent, VendorMatch)
from app.pipeline import runner
from app.pipeline.stages import s06_selfcheck
from app.seed import seed

TODAY = date(2026, 11, 16)


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "test.db"
    seed(path)
    return path


@pytest.fixture(scope="session")
def ref(seeded_db):
    return runner.load_reference(seeded_db)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch) -> Path:
    """A per-test seeded DB with uploads redirected away from data/."""
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    path = tmp_path / "run.db"
    seed(path)
    return path


def _field(value, conf=0.98):
    return {"value": value, "confidence": conf, "evidence": value, "page": 1}


def make_invoice(**changes) -> NormalisedInvoice:
    """The happy-path invoice (HP_nordic_clean.pdf) as S05 would produce it."""
    inv = NormalisedInvoice(
        document_type="tax_invoice", invoice_number="INV-NC-5531", invoice_date=date(2026, 11, 2),
        invoice_date_alternatives=[], due_date=date(2026, 12, 2), currency="INR", currency_source="iso_code",
        supplier_name="Nordic Components Pvt Ltd", supplier_name_canonical="NORDIC COMPONENTS",
        supplier_tax_id="29AABCN4471K1Z3", supplier_email="accounts@nordiccomponents.in",
        buyer_name="Halden Precision Engineering Pvt Ltd", buyer_tax_id="29AAECH0007L1Z2",
        po_refs=[PoRef("PO-2041", "PO-2041", True, "extraction")],
        lines=[InvoiceLine(1, "Deep groove ball bearing 6204-2RS", "BRG-12", Decimal(60), "NOS", 75000, Decimal(18), 4500000),
               InvoiceLine(2, "Precision ground shaft 20mm x 300mm", "SHF-20", Decimal(20), "NOS", 150000, Decimal(18), 3000000),
               InvoiceLine(3, "Nitrile oil seal 35x52x8", "SEAL-8", Decimal(100), "NOS", 5000, Decimal(18), 500000)],
        subtotal_minor=8000000, discount_minor=None, tax_total_minor=None,
        tax_breakdown=[TaxComponent("CGST @ 9%", 720000, Decimal(9)), TaxComponent("SGST @ 9%", 720000, Decimal(9))],
        shipping_minor=85000, grand_total_minor=9525000, effective_tax_rate_pct=Decimal("18.00"),
        bank_account_last4="4471", bank_ifsc="HDFC0000523", beneficiary_name="Nordic Components Pvt Ltd",
        confidence={"invoice_number": 0.99, "invoice_date": 0.98, "supplier.name": 0.99,
                    "totals.grand_total": 0.99, "currency": 0.97},
        human_supplied=[], warnings=[])
    for k, v in changes.items():
        setattr(inv, k, v)
    return inv


@pytest.fixture
def happy_ctx(ref) -> RunContext:
    ctx = RunContext(run_id="test", db_path=None, today=TODAY, source_filename="HP.pdf", file_bytes=b"",
                     persist=False)
    ctx.ref = ref
    ctx.invoice = make_invoice()
    ctx.extraction = {"currency": _field("INR", 0.97), "invoice_number": _field("INV-NC-5531"),
                      "unreadable_regions": []}
    ctx.vendor_match = VendorMatch("V001", "exact", 100.0, False, [])
    ctx.po_match = POMatch("T1", "PO-2041", "PO-2041", [], "quoted and owned")
    ctx.consumption = Consumption("PO-2041", 60000000, 45000000, 15000000,
                                  [{"invoice_number": "INV-NC-5501", "invoice_date": "2026-09-12",
                                    "total_minor": 24000000, "status": "approved"},
                                   {"invoice_number": "INV-NC-5502", "invoice_date": "2026-10-24",
                                    "total_minor": 21000000, "status": "approved"}], 9525000)
    ctx.grn = GrnCheck("PO-2041", ["GRN-7101", "GRN-7135"],
                       [{"sku": "BRG-12", "invoiced": "60", "received": "300", "short_by": "0"},
                        {"sku": "SHF-20", "invoiced": "20", "received": "80", "short_by": "0"},
                        {"sku": "SEAL-8", "invoiced": "100", "received": "800", "short_by": "0"}], [])
    ctx.duplicates = DuplicateCheck([], [])
    ctx.selfcheck = s06_selfcheck.check(ctx)
    return ctx
