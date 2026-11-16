"""Domain dataclasses shared across stages, rules and the API.

Money fields end in `_minor` and are always int. Quantities and tax rates are
Decimal. Nothing here does I/O.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

StageStatus = Literal["pending", "running", "ok", "warn", "failed", "skipped"]
RuleStatus = Literal["pass", "warn", "fail", "not_applicable"]
Severity = Literal["hard", "soft", "info"]
Verdict = Literal["AUTO_APPROVE", "APPROVE_PENDING_SIGNOFF", "HOLD_FOR_REVIEW", "BLOCK", "REJECT"]


# ---------------------------------------------------------------- reference data

@dataclass(frozen=True)
class Vendor:
    id: str
    legal_name: str
    aliases: tuple[str, ...]
    status: str
    tax_id: str | None
    bank_account_last4: str | None
    bank_ifsc: str | None
    payment_terms_days: int
    default_currency: str
    email_domain: str | None = None      # the domain the vendor's invoices normally come from


@dataclass(frozen=True)
class POLine:
    sku: str
    description: str
    qty: Decimal
    uom: str
    unit_price_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class PurchaseOrder:
    id: str
    po_number: str
    vendor_id: str
    currency: str
    po_total_minor: int
    status: str
    allow_partial_invoicing: bool
    issue_date: date
    expiry_date: date | None
    lines: tuple[POLine, ...]


@dataclass(frozen=True)
class GoodsReceipt:
    id: str
    po_id: str
    grn_number: str
    received_date: date
    lines: tuple[tuple[str, Decimal], ...]  # (sku, qty_received)


@dataclass(frozen=True)
class ReferenceData:
    """Master data loaded once per run, so rules can stay pure functions of ctx."""
    vendors: dict[str, Vendor]
    pos: dict[str, PurchaseOrder]                 # by po_number
    grns_by_po_id: dict[str, tuple[GoodsReceipt, ...]]


# ---------------------------------------------------------------- normalised invoice

@dataclass
class InvoiceLine:
    line_no: int
    description: str | None
    sku: str | None
    qty: Decimal | None
    uom: str | None
    unit_price_minor: int | None
    tax_rate_pct: Decimal | None
    line_total_minor: int | None


@dataclass
class TaxComponent:
    label: str
    amount_minor: int | None
    rate_pct: Decimal | None


@dataclass
class PoRef:
    value: str        # canonical, e.g. "PO-2041"
    raw: str          # as found
    explicit: bool    # printed as a PO reference, vs implied in free text
    source: str       # "extraction" | "document_text" | "field:<path>"


@dataclass
class NormalisedInvoice:
    document_type: str
    invoice_number: str | None
    invoice_date: date | None
    invoice_date_alternatives: list[date]   # >1 entry when DD/MM vs MM/DD stayed ambiguous
    due_date: date | None
    currency: str | None
    currency_source: str | None             # symbol | iso_code | vendor_default
    supplier_name: str | None
    supplier_name_canonical: str | None
    supplier_tax_id: str | None
    supplier_email: str | None
    buyer_name: str | None
    buyer_tax_id: str | None
    po_refs: list[PoRef]
    lines: list[InvoiceLine]
    subtotal_minor: int | None
    discount_minor: int | None
    tax_total_minor: int | None
    tax_breakdown: list[TaxComponent]
    shipping_minor: int | None
    grand_total_minor: int | None
    effective_tax_rate_pct: Decimal | None
    bank_account_last4: str | None           # the full number is never kept
    bank_ifsc: str | None
    beneficiary_name: str | None
    confidence: dict[str, float]             # field path -> reading confidence
    human_supplied: list[str]                # field paths typed in by a clerk
    warnings: list[str]


# ---------------------------------------------------------------- match results

@dataclass
class VendorMatch:
    vendor_id: str | None
    method: str            # exact | alias | tax_id | fuzzy | none
    score: float           # 0-100
    ambiguous: bool
    candidates: list[dict[str, Any]]


@dataclass
class POMatch:
    tier: str | None               # T1 | T2 | T3 | None
    po_number: str | None          # the matched PO (T1/T3 only)
    quoted_po_number: str | None   # the PO printed on the invoice, even when not matched
    candidates: list[dict[str, Any]]
    reason: str


@dataclass
class Consumption:
    po_number: str
    po_total_minor: int
    consumed_minor: int
    remaining_minor: int
    prior_invoices: list[dict[str, Any]]
    this_invoice_minor: int | None


@dataclass
class GrnCheck:
    po_number: str
    grn_numbers: list[str]
    lines: list[dict[str, Any]]        # sku, invoiced, received, short_by
    unmatched_lines: list[dict[str, Any]]


@dataclass
class DuplicateCheck:
    exact: list[dict[str, Any]]
    near: list[dict[str, Any]]


# ---------------------------------------------------------------- stages / rules / decision

@dataclass
class StageResult:
    status: StageStatus
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleResult:
    status: RuleStatus
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)
    # Filled in by the engine from the rule's registration:
    code: str = ""
    name: str = ""
    category: str = ""
    severity: Severity = "soft"


@dataclass
class Decision:
    verdict: Verdict
    reason_codes: list[str]
    explanation: str
    next_action: str
    owner: str
    branch: str                      # which matrix branch fired, in words
    payment_date: date | None = None


@dataclass
class RunContext:
    """Everything one run knows. Stages write facts onto it; rules only read it."""
    run_id: str
    db_path: Path | None
    today: date
    source_filename: str
    file_bytes: bytes
    persist: bool = True                         # False for a dry-run replay
    overrides: dict[str, str] = field(default_factory=dict)   # field path -> human value

    # S01
    sha256: str | None = None
    mime: str | None = None
    stored_path: Path | None = None
    page_count: int | None = None
    file_meta: dict[str, Any] = field(default_factory=dict)
    # S02 / S03 — word boxes are {"text","conf","page","bbox":[x0,y0,x1,y1]} normalised 0-1
    text_by_page: list[str] = field(default_factory=list)
    word_boxes: list[dict[str, Any]] = field(default_factory=list)
    is_scanned: bool = False
    image_only_pages: list[int] = field(default_factory=list)
    ocr_text_by_page: list[str] = field(default_factory=list)
    ocr_unavailable: bool = False
    legibility_by_page: dict[int, float] = field(default_factory=dict)
    processed_page_paths: list[str] = field(default_factory=list)
    page_images: list[Any] = field(default_factory=list)   # PIL images, not persisted
    # S04
    extraction: dict[str, Any] | None = None
    raw_llm: dict[str, Any] | None = None
    extraction_source: str | None = None        # live | cache
    # S05 / S06
    invoice: NormalisedInvoice | None = None
    selfcheck: dict[str, Any] = field(default_factory=dict)
    # S07
    ref: ReferenceData | None = None
    vendor_match: VendorMatch | None = None
    po_match: POMatch | None = None
    consumption: Consumption | None = None
    grn: GrnCheck | None = None
    grn_po_has_receipts: bool = False
    duplicates: DuplicateCheck | None = None
    # S08 / S09
    rule_results: list[RuleResult] = field(default_factory=list)
    decision: Decision | None = None
    # runner bookkeeping
    t0: float = 0.0                                                # perf_counter at execution start
    stage_warnings: dict[str, str] = field(default_factory=dict)   # stage_key -> summary
    failed_stage: str | None = None
    failure_error: str | None = None


def to_jsonable(obj: Any) -> Any:
    """Dataclasses / Decimal / date / Path -> plain JSON types. Money stays int."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return obj
