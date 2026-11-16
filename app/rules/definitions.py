"""R01..R14: one pure function per AP control.

Each message carries the actual numbers, because the message is what a clerk
reads. Comments give the AP reasoning behind a rule, not a restatement of it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app import config
from app.models import PurchaseOrder, RuleResult, RunContext
from app.rules.engine import failed, not_applicable, passed, rule, warned
from app.services.money import format_money, po_tolerance_minor


# ---------------------------------------------------------------- helpers (read-only)

def _cur(ctx: RunContext) -> str:
    return ctx.invoice.currency or "INR"


def _matched_po(ctx: RunContext) -> PurchaseOrder | None:
    """The PO this invoice is matched to (T1 explicit or T3 implied)."""
    pm = ctx.po_match
    return ctx.ref.pos.get(pm.po_number) if pm and pm.po_number else None


def _referenced_po(ctx: RunContext) -> PurchaseOrder | None:
    """Matched PO, or the PO quoted on the invoice even if it belongs to someone else (T2).

    Status and currency are facts about the document the vendor sent, so they
    are checked against what was quoted — that is how Edge 6 reports all three
    hard failures instead of stopping at the first.
    """
    po = _matched_po(ctx)
    if po:
        return po
    pm = ctx.po_match
    return ctx.ref.pos.get(pm.quoted_po_number) if pm and pm.tier == "T2" and pm.quoted_po_number else None


def _vendor(ctx: RunContext):
    vm = ctx.vendor_match
    return ctx.ref.vendors.get(vm.vendor_id) if vm and vm.vendor_id else None


def _pct(part: int, whole: int) -> str:
    return f"{(Decimal(part) * 100 / Decimal(whole)).quantize(Decimal('0.1'))}%" if whole else "n/a"


_REQUIRED_VALUE = {
    "invoice_number": lambda inv: inv.invoice_number,
    "invoice_date": lambda inv: inv.invoice_date,
    "supplier.name": lambda inv: inv.supplier_name,
    "totals.grand_total": lambda inv: inv.grand_total_minor,
    "currency": lambda inv: inv.currency,
}


def required_field_confidences(ctx: RunContext) -> dict[str, float]:
    """Reading confidence of each required field that has a value (clerk-supplied = 1.0)."""
    inv = ctx.invoice
    out: dict[str, float] = {}
    for f in config.REQUIRED_FIELDS:
        if _REQUIRED_VALUE[f](inv) is None:
            continue
        if f == "currency":
            if inv.currency_source == "vendor_default":
                continue  # not read from the document at all; R01 accepts the master default
            raw = (ctx.extraction or {}).get("currency", {}).get("value")
            out[f] = inv.confidence.get("currency" if raw else "totals.grand_total", 0.0)
            continue
        out[f] = 1.0 if f in inv.human_supplied else inv.confidence.get(f, 0.0)
    return out


def run_confidence(ctx: RunContext) -> float | None:
    """runs.confidence: the weakest required-field reading."""
    conf = required_field_confidences(ctx)
    return round(min(conf.values()), 3) if conf else None


# ---------------------------------------------------------------- completeness

# Without these five, a payment cannot be posted, matched or audited.
@rule("R01_REQUIRED_FIELDS", "Required fields present", "completeness", "hard")
def r01_required_fields(ctx: RunContext) -> RuleResult:
    inv = ctx.invoice
    missing = [f for f in config.REQUIRED_FIELDS if _REQUIRED_VALUE[f](inv) is None]
    if not missing:
        return passed(f"All {len(config.REQUIRED_FIELDS)} required fields present: "
                      f"{', '.join(config.REQUIRED_FIELDS)}")
    detail = []
    for f in missing:
        node = ctx.extraction or {}
        for part in f.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        raw = node.get("value") if isinstance(node, dict) else None
        detail.append(f"{f} (printed as {raw!r} but not parseable)" if raw else f"{f} (absent or unreadable)")
    return failed(f"Missing required field{'s' if len(missing) > 1 else ''}: {'; '.join(detail)}",
                  [f"REQUIRED_FIELD_MISSING:{f}" for f in missing], missing=missing,
                  unreadable_regions=(ctx.extraction or {}).get("unreadable_regions", []))


# ---------------------------------------------------------------- integrity

# Soft: a mismatch is as often a misread as a vendor error; a human checks the page.
@rule("R02_ARITHMETIC_INTEGRITY", "Document arithmetic adds up", "integrity", "soft")
def r02_arithmetic(ctx: RunContext) -> RuleResult:
    checks = ctx.selfcheck.get("checks", [])
    if not checks:
        return not_applicable("Not enough figures printed to cross-check")
    bad = ctx.selfcheck["discrepancies"]
    tol = format_money(config.ROUNDING_TOLERANCE_MINOR, _cur(ctx))
    if not bad:
        return passed(f"{len(checks)} arithmetic checks agree within {tol}", checks=len(checks))
    return failed("; ".join(f"{c['message']} — {c['suspect']}" for c in bad) + f" (tolerance {tol})",
                  ["ARITHMETIC_MISMATCH"], discrepancies=bad)


@rule("R09_DATE_SANITY", "Invoice date is plausible", "integrity", "soft")
def r09_date_sanity(ctx: RunContext) -> RuleResult:
    inv = ctx.invoice
    if inv.invoice_date is None:
        return not_applicable("No invoice date to check (see R01)")
    if inv.invoice_date_alternatives:
        readings = " or ".join(d.isoformat() for d in inv.invoice_date_alternatives)
        return failed(f"Invoice date is ambiguous: could be {readings}; the due date and payment run depend on it",
                      ["DATE_AMBIGUOUS"], readings=[d.isoformat() for d in inv.invoice_date_alternatives])
    problems = []
    po = _referenced_po(ctx)
    if po and inv.invoice_date < po.issue_date:
        problems.append(f"dated {inv.invoice_date.isoformat()}, before {po.po_number} was issued on "
                        f"{po.issue_date.isoformat()} (goods invoiced before they were ordered)")
    if inv.invoice_date > ctx.today + timedelta(days=config.FUTURE_DATE_GRACE_DAYS):
        problems.append(f"dated {inv.invoice_date.isoformat()}, more than {config.FUTURE_DATE_GRACE_DAYS} day "
                        f"in the future (today {ctx.today.isoformat()})")
    age = (ctx.today - inv.invoice_date).days
    if age > config.STALE_INVOICE_DAYS:
        problems.append(f"{age} days old, beyond the {config.STALE_INVOICE_DAYS}-day staleness limit")
    if problems:
        return failed("Invoice " + "; ".join(problems), ["DATE_INVALID"],
                      invoice_date=inv.invoice_date.isoformat(), today=ctx.today.isoformat())
    return passed(f"Dated {inv.invoice_date.isoformat()} ({age} days ago)"
                  + (f", after {po.po_number} issue date {po.issue_date.isoformat()}" if po else ""))


@rule("R10_TAX_PLAUSIBILITY", "Tax rate and breakdown plausible", "integrity", "soft")
def r10_tax(ctx: RunContext) -> RuleResult:
    inv = ctx.invoice
    allowed = config.ALLOWED_TAX_RATES
    pp = Decimal(str(config.TAX_RATE_TOLERANCE_PP))
    problems = []

    def near_slab(rate: Decimal) -> bool:
        return any(abs(rate - Decimal(s)) <= pp for s in allowed)

    line_rates = [l.tax_rate_pct for l in inv.lines if l.tax_rate_pct is not None]
    for l in inv.lines:
        if l.tax_rate_pct is not None and not near_slab(l.tax_rate_pct):
            problems.append(f"line {l.line_no} rate {l.tax_rate_pct}% is not a GST slab {allowed}")
    # With per-line rates printed, mixed-rate invoices have a blended effective
    # rate between slabs by design, so only check the blend when lines carry no rate.
    if not line_rates and inv.effective_tax_rate_pct is not None and not near_slab(inv.effective_tax_rate_pct):
        problems.append(f"effective tax rate {inv.effective_tax_rate_pct}% is not within "
                        f"{config.TAX_RATE_TOLERANCE_PP}pp of any GST slab {allowed}")
    parts = [b.amount_minor for b in inv.tax_breakdown]
    if inv.tax_total_minor is not None and parts and all(p is not None for p in parts):
        if abs(sum(parts) - inv.tax_total_minor) > config.ROUNDING_TOLERANCE_MINOR:
            problems.append(f"stated tax {format_money(inv.tax_total_minor, _cur(ctx))} ≠ sum of breakdown "
                            f"{format_money(sum(parts), _cur(ctx))}")
    if problems:
        return failed("; ".join(problems), ["TAX_IMPLAUSIBLE"],
                      effective_rate_pct=str(inv.effective_tax_rate_pct), allowed=list(allowed))
    if inv.effective_tax_rate_pct is None and not line_rates:
        return not_applicable("No tax figures printed to check")
    labels = ", ".join(b.label for b in inv.tax_breakdown) or "no breakdown"
    return passed(f"Effective tax rate {inv.effective_tax_rate_pct}% ({labels}) matches a GST slab")


# ---------------------------------------------------------------- vendor

# Hard: paying a party that is not approved is a compliance breach whatever the amount.
@rule("R03_VENDOR_APPROVED", "Vendor is approved", "vendor", "hard")
def r03_vendor_approved(ctx: RunContext) -> RuleResult:
    vm, v = ctx.vendor_match, _vendor(ctx)
    name = ctx.invoice.supplier_name
    if v is None:
        best = vm.candidates[0] if vm and vm.candidates else None
        hint = f"; closest is {best['vendor_id']} {best['legal_name']} at {best['score']:g}" if best else ""
        return failed(f"Supplier {name!r} is not in the vendor master{hint} "
                      f"(needs ≥ {config.VENDOR_MATCH_AMBIGUOUS_MIN} to match)",
                      ["VENDOR_NOT_APPROVED"], supplier=name, candidates=vm.candidates if vm else [])
    if v.status != "approved":
        return failed(f"Vendor {v.id} {v.legal_name} is '{v.status}' in the vendor master",
                      ["VENDOR_NOT_APPROVED"], vendor_id=v.id, status=v.status)
    return passed(f"{v.id} {v.legal_name} is approved (matched by {vm.method})", vendor_id=v.id)


@rule("R03b_VENDOR_AMBIGUOUS", "Vendor match is unambiguous", "vendor", "soft")
def r03b_vendor_ambiguous(ctx: RunContext) -> RuleResult:
    vm = ctx.vendor_match
    if not vm or not vm.vendor_id:
        return not_applicable("No vendor matched (see R03)")
    if vm.ambiguous:
        return failed(f"Supplier {ctx.invoice.supplier_name!r} matched {vm.vendor_id} with fuzzy score "
                      f"{vm.score:g}, between {config.VENDOR_MATCH_AMBIGUOUS_MIN} and {config.VENDOR_MATCH_MIN}; "
                      "confirm the vendor", ["VENDOR_AMBIGUOUS"], candidates=vm.candidates)
    return passed(f"Matched {vm.vendor_id} by {vm.method}" + (f" (score {vm.score:g})" if vm.method == "fuzzy" else ""))


# ---------------------------------------------------------------- duplicates

# Hard and BLOCK: paying twice is the costliest clerical error in AP.
@rule("R04_DUPLICATE_EXACT", "Not an exact duplicate", "duplicate", "hard")
def r04_duplicate_exact(ctx: RunContext) -> RuleResult:
    exact = ctx.duplicates.exact if ctx.duplicates else []
    if not exact:
        return passed("No earlier invoice with the same file hash or the same vendor + invoice number")
    first = exact[0]
    if first["kind"] == "same_vendor_and_number":
        what = (f"invoice {first['invoice_number']} from this vendor was already processed on "
                f"{first['invoice_date']} ({first['status']}, {format_money(first['total_minor'], _cur(ctx))})")
    elif first["kind"] == "same_file_in_history":
        what = f"the identical file was already processed as {first['invoice_number']} ({first['status']})"
    else:
        what = f"the identical file was already approved in run {first['run_id']} ({first['decision']})"
    return failed(f"Exact duplicate: {what}", ["DUPLICATE_EXACT"], matches=exact)


# Soft, not hard: vendors legitimately void and re-issue invoices, and
# auto-rejecting those damages the relationship. A human decides.
@rule("R04b_DUPLICATE_NEAR", "Not a near-duplicate", "duplicate", "soft")
def r04b_duplicate_near(ctx: RunContext) -> RuleResult:
    near = ctx.duplicates.near if ctx.duplicates else []
    inv = ctx.invoice
    if not near:
        return passed(f"No invoice from this vendor for the same amount within ±{config.DUP_DATE_WINDOW_DAYS} days")
    p = near[0]
    gap = abs((inv.invoice_date - date.fromisoformat(p["invoice_date"])).days)
    return failed(f"{inv.invoice_number} looks like a re-issue of {p['invoice_number']} ({p['status']}): same vendor, "
                  f"same total {format_money(p['total_minor'], _cur(ctx))}, dated {gap} day{'s' if gap != 1 else ''} "
                  f"apart, different invoice number", ["POSSIBLE_DUPLICATE"], suspects=near)


# ---------------------------------------------------------------- PO

# Soft: non-PO spend is legitimate below a ceiling (R05b enforces the ceiling).
@rule("R05_PO_RESOLVED", "Matched to a PO", "po", "soft")
def r05_po_resolved(ctx: RunContext) -> RuleResult:
    pm = ctx.po_match
    if pm.tier in ("T1", "T3"):
        return passed(f"Matched {pm.po_number} ({'explicit reference' if pm.tier == 'T1' else 'implied match'}: "
                      f"{pm.reason})", tier=pm.tier)
    if pm.tier == "T2":
        return not_applicable(f"{pm.quoted_po_number} was quoted but belongs to another vendor (see R05c)")
    return failed(f"No PO matched: {pm.reason}", ["PO_NOT_FOUND"], candidates=pm.candidates)


@rule("R05b_PO_REQUIRED_FOR_AMOUNT", "PO present above non-PO ceiling", "po", "hard")
def r05b_po_required(ctx: RunContext) -> RuleResult:
    pm, inv = ctx.po_match, ctx.invoice
    ceiling = format_money(config.NON_PO_SPEND_CEILING_MINOR, "INR")
    if pm.tier in ("T1", "T3", "T2"):
        return not_applicable("A PO is quoted or matched")
    if inv.grand_total_minor is None:
        return not_applicable("No grand total (see R01)")
    if inv.currency != "INR":
        # The ceiling is an INR policy and we hold no FX rates: foreign spend always needs a PO.
        return failed(f"No PO and the invoice is in {inv.currency}; non-PO spend is only allowed in INR "
                      f"up to {ceiling}", ["PO_REQUIRED_FOR_AMOUNT"])
    if inv.grand_total_minor > config.NON_PO_SPEND_CEILING_MINOR:
        return failed(f"No PO for {format_money(inv.grand_total_minor)}, above the {ceiling} non-PO ceiling",
                      ["PO_REQUIRED_FOR_AMOUNT"], total_minor=inv.grand_total_minor)
    return passed(f"Non-PO spend {format_money(inv.grand_total_minor)} is within the {ceiling} ceiling")


# Hard: a PO is an authorisation granted to one vendor; another vendor quoting
# it is either an error or an attempt to ride someone else's approval.
@rule("R05c_PO_VENDOR_MISMATCH", "PO belongs to this vendor", "po", "hard")
def r05c_po_vendor_mismatch(ctx: RunContext) -> RuleResult:
    pm = ctx.po_match
    if pm.tier != "T2":
        return not_applicable("No foreign PO quoted") if pm.tier is None else \
            passed(f"{pm.po_number} belongs to this vendor")
    po = ctx.ref.pos[pm.quoted_po_number]
    owner = ctx.ref.vendors.get(po.vendor_id)
    v = _vendor(ctx)
    return failed(f"{po.po_number} belongs to {po.vendor_id} {owner.legal_name if owner else ''}, but this invoice is "
                  f"from {v.id + ' ' + v.legal_name if v else repr(ctx.invoice.supplier_name)}",
                  ["PO_VENDOR_MISMATCH"], po_number=po.po_number, po_vendor=po.vendor_id,
                  invoice_vendor=v.id if v else None)


@rule("R05d_PO_STATUS_INVALID", "PO is open and in date", "po", "hard")
def r05d_po_status(ctx: RunContext) -> RuleResult:
    po = _referenced_po(ctx)
    if po is None:
        return not_applicable("No PO to check")
    problems = []
    if po.status in ("closed", "cancelled"):
        problems.append(f"{po.po_number} is {po.status}")
    as_of = ctx.invoice.invoice_date or ctx.today
    if po.expiry_date and as_of > po.expiry_date:
        problems.append(f"{po.po_number} expired on {po.expiry_date.isoformat()} "
                        f"(invoice dated {as_of.isoformat()})")
    if problems:
        return failed("; ".join(problems), ["PO_STATUS_INVALID"], status=po.status,
                      expiry_date=po.expiry_date.isoformat() if po.expiry_date else None)
    return passed(f"{po.po_number} is {po.status}"
                  + (f", valid until {po.expiry_date.isoformat()}" if po.expiry_date else ""))


@rule("R06_PO_AMOUNT_TOLERANCE", "Amount within PO tolerance", "po", "soft")
def r06_po_amount(ctx: RunContext) -> RuleResult:
    po, c, inv = _matched_po(ctx), ctx.consumption, ctx.invoice
    if po is None or c is None:
        return not_applicable("No matched PO")
    if inv.grand_total_minor is None:
        return not_applicable("No grand total (see R01)")
    if inv.currency != po.currency:
        return not_applicable(f"Currencies differ ({inv.currency} vs {po.currency}); see R11")
    cur, total, remaining = po.currency, inv.grand_total_minor, c.remaining_minor
    tol = po_tolerance_minor(po.po_total_minor)
    delta = total - remaining
    band = (f"tolerance {format_money(tol, cur)} = min(max({config.PO_TOLERANCE_PCT:.1%} of "
            f"{format_money(po.po_total_minor, cur)}, {format_money(config.PO_TOLERANCE_FLOOR_MINOR, cur)}), "
            f"{format_money(config.PO_TOLERANCE_CEIL_MINOR, cur)})")
    ev = {"invoice_minor": total, "po_remaining_minor": remaining, "delta_minor": delta, "tolerance_minor": tol,
          "allow_partial": po.allow_partial_invoicing}
    # On a PO that allows partial invoicing, billing less than what remains is
    # the normal case; only over-billing is a variance. See ASSUMPTIONS.md.
    exceeds = delta > tol if po.allow_partial_invoicing else abs(delta) > tol
    if exceeds:
        return failed(f"PO amount variance {_pct(delta, remaining)} exceeds the {band} (invoice "
                      f"{format_money(total, cur)} vs PO remaining {format_money(remaining, cur)}, "
                      f"delta {format_money(delta, cur)})", ["PO_AMOUNT_TOLERANCE"], **ev)
    if po.allow_partial_invoicing and delta < 0:
        return passed(f"Invoice {format_money(total, cur)} fits within {po.po_number}'s remaining "
                      f"{format_money(remaining, cur)} (partial invoicing allowed)", **ev)
    return passed(f"Invoice {format_money(total, cur)} vs PO remaining {format_money(remaining, cur)}: "
                  f"delta {format_money(delta, cur)} within {band}", **ev)


# Hard: paying past the PO ceiling means paying more than anyone authorised.
# Applies when earlier invoices already consumed the PO — the split-invoice
# case a one-to-one matcher cannot see. A single invoice over its PO is a
# price variance and is R06's (soft) job. See ASSUMPTIONS.md.
@rule("R07_PO_OVER_CONSUMPTION", "Cumulative invoicing within PO", "po", "hard")
def r07_po_over_consumption(ctx: RunContext) -> RuleResult:
    po, c, inv = _matched_po(ctx), ctx.consumption, ctx.invoice
    if po is None or c is None:
        return not_applicable("No matched PO")
    if inv.grand_total_minor is None or inv.currency != po.currency:
        return not_applicable("No comparable grand total")
    cur = po.currency
    if not c.prior_invoices:
        return not_applicable(f"No earlier invoices against {po.po_number}; single-invoice variance is checked by R06")
    tol = po_tolerance_minor(po.po_total_minor)
    cumulative = c.consumed_minor + inv.grand_total_minor
    priors = ", ".join(f"{p['invoice_number']} {format_money(p['total_minor'], cur)}" for p in c.prior_invoices)
    ev = {"po_total_minor": po.po_total_minor, "consumed_minor": c.consumed_minor,
          "this_invoice_minor": inv.grand_total_minor, "cumulative_minor": cumulative,
          "tolerance_minor": tol, "prior_invoices": c.prior_invoices}
    if cumulative > po.po_total_minor + tol:
        over = cumulative - po.po_total_minor
        return failed(f"Cumulative invoicing on {po.po_number} would reach {format_money(cumulative, cur)} against "
                      f"an authorised {format_money(po.po_total_minor, cur)} — {format_money(over, cur)} over "
                      f"(prior: {priors}; this invoice {format_money(inv.grand_total_minor, cur)})",
                      ["PO_OVER_CONSUMPTION"], overage_minor=over, **ev)
    return passed(f"Prior {priors} + this {format_money(inv.grand_total_minor, cur)} = "
                  f"{format_money(cumulative, cur)}, within {format_money(po.po_total_minor, cur)} "
                  f"(+{format_money(tol, cur)} tolerance)", **ev)


@rule("R07b_PARTIAL_NOT_ALLOWED", "Partial invoice permitted", "po", "soft")
def r07b_partial(ctx: RunContext) -> RuleResult:
    po, inv = _matched_po(ctx), ctx.invoice
    if po is None or inv.grand_total_minor is None or inv.currency != po.currency:
        return not_applicable("No comparable PO")
    if po.allow_partial_invoicing:
        return passed(f"{po.po_number} allows partial invoicing")
    tol = po_tolerance_minor(po.po_total_minor)
    if inv.grand_total_minor < po.po_total_minor - tol:
        return failed(f"Invoice {format_money(inv.grand_total_minor, po.currency)} is less than {po.po_number}'s "
                      f"{format_money(po.po_total_minor, po.currency)}, and this PO does not allow partial invoicing",
                      ["PARTIAL_NOT_ALLOWED"])
    return passed(f"Invoice covers the full {po.po_number} amount")


# Three-way match: never pay for goods the stores team has not received.
@rule("R08_GRN_COVERAGE", "Goods received for invoiced quantities", "po", "soft")
def r08_grn(ctx: RunContext) -> RuleResult:
    po, g = _matched_po(ctx), ctx.grn
    if po is None or g is None:
        return not_applicable("No matched PO")
    if not g.grn_numbers:
        return failed(f"No goods receipt recorded against {po.po_number}; nothing has been received to pay for",
                      ["GRN_MISSING"], po_number=po.po_number)
    short = [l for l in g.lines if Decimal(l["short_by"]) > 0]
    if short:
        return failed("Invoiced quantity exceeds received: " + "; ".join(
            f"{l['sku']} invoiced {l['invoiced']}, received {l['received']} (short {l['short_by']})" for l in short)
            + f" [GRN {', '.join(g.grn_numbers)}]", ["GRN_SHORT_RECEIPT"], lines=g.lines)
    if g.unmatched_lines:
        return warned(f"{len(g.unmatched_lines)} invoice line(s) could not be mapped to a {po.po_number} SKU: "
                      + "; ".join(str(u["description"]) for u in g.unmatched_lines), lines=g.lines,
                      unmatched=g.unmatched_lines)
    return passed("All invoiced quantities covered by goods receipts: " + "; ".join(
        f"{l['sku']} {l['invoiced']} of {l['received']} received" for l in g.lines)
        + f" [GRN {', '.join(g.grn_numbers)}]", lines=g.lines)


@rule("R11_CURRENCY_MATCH", "Currency matches PO", "po", "hard")
def r11_currency(ctx: RunContext) -> RuleResult:
    po, inv = _referenced_po(ctx), ctx.invoice
    if po is None:
        return not_applicable("No PO to compare")
    if inv.currency is None:
        return not_applicable("No currency (see R01)")
    if inv.currency != po.currency:
        return failed(f"Invoice is in {inv.currency} but {po.po_number} is in {po.currency}",
                      ["CURRENCY_MISMATCH"], invoice_currency=inv.currency, po_currency=po.currency)
    return passed(f"Invoice and {po.po_number} are both in {po.currency}")


# ---------------------------------------------------------------- risk

# The highest-value rule in the system. Invoice-redirection fraud is the largest
# AP loss vector, and a fraudulent invoice is usually perfect on every other
# check. It blocks regardless of how well everything else matched.
@rule("R12_BANK_DETAILS_UNCHANGED", "Bank details match vendor master", "risk", "hard")
def r12_bank(ctx: RunContext) -> RuleResult:
    v, inv = _vendor(ctx), ctx.invoice
    if v is None:
        return not_applicable("No vendor master record to compare against (see R03)")
    domain = inv.supplier_email.rsplit("@", 1)[-1].lower() if inv.supplier_email and "@" in inv.supplier_email \
        else None
    domain_note = (f"; the invoice comes from {domain}, not {v.email_domain} on the vendor master"
                   if domain and v.email_domain and domain != v.email_domain.lower() else "")
    dom_ev = {"invoice_email_domain": domain, "master_email_domain": v.email_domain,
              "email_domain_mismatch": bool(domain_note)}
    if not inv.bank_account_last4 and not inv.bank_ifsc:
        if domain_note:
            return warned("No bank details printed" + domain_note, **dom_ev)
        return passed("No bank details printed; payment would go to the account on the vendor master", **dom_ev)
    diffs = []
    if inv.bank_account_last4 and v.bank_account_last4 and inv.bank_account_last4 != v.bank_account_last4:
        diffs.append(f"account ending {inv.bank_account_last4} vs master …{v.bank_account_last4}")
    if inv.bank_ifsc and v.bank_ifsc and inv.bank_ifsc != v.bank_ifsc.upper():
        diffs.append(f"IFSC/SWIFT {inv.bank_ifsc} vs master {v.bank_ifsc}")
    ev = {"invoice_last4": inv.bank_account_last4, "master_last4": v.bank_account_last4,
          "invoice_ifsc": inv.bank_ifsc, "master_ifsc": v.bank_ifsc, **dom_ev}
    if diffs:
        return failed("Bank details differ from the vendor master: " + "; ".join(diffs) + domain_note
                      + " — possible invoice-redirection fraud", ["BANK_DETAILS_CHANGED"], **ev)
    if domain_note:
        # Same bank, odd sender: worth a look, not a block. Bank details decide.
        return warned(f"Bank details match the vendor master{domain_note}", **ev)
    return passed(f"Account …{inv.bank_account_last4 or v.bank_account_last4} / {inv.bank_ifsc or v.bank_ifsc} "
                  "matches the vendor master", **ev)


# ---------------------------------------------------------------- confidence

@rule("R13_EXTRACTION_CONFIDENCE", "Extraction confidence sufficient", "confidence", "soft")
def r13_confidence(ctx: RunContext) -> RuleResult:
    conf = required_field_confidences(ctx)
    low = {f: c for f, c in conf.items() if c < config.CONFIDENCE_MIN}
    illegible = {p: s for p, s in ctx.legibility_by_page.items() if s < config.OCR_LEGIBILITY_MIN}
    problems = [f"{f} read at {c:.2f}" for f, c in low.items()]
    problems += [f"page {p} legibility {s:.2f} < {config.OCR_LEGIBILITY_MIN}" for p, s in illegible.items()]
    if problems:
        return failed(f"Below the {config.CONFIDENCE_MIN} confidence floor: " + "; ".join(problems),
                      ["LOW_CONFIDENCE"], confidences=conf, legibility=ctx.legibility_by_page)
    if not conf:
        return not_applicable("No required fields read")
    weakest = min(conf, key=conf.get)
    return passed(f"Lowest required-field confidence {conf[weakest]:.2f} ({weakest}) ≥ {config.CONFIDENCE_MIN}",
                  confidences=conf)


@rule("R14_UNREADABLE_REGIONS", "No unreadable region hides a required field", "confidence", "soft")
def r14_unreadable(ctx: RunContext) -> RuleResult:
    regions = (ctx.extraction or {}).get("unreadable_regions", [])
    if not regions:
        return passed("The model reported no unreadable regions")
    conf = required_field_confidences(ctx)
    affected = [f for f in config.REQUIRED_FIELDS
                if f not in ctx.invoice.human_supplied
                and (_REQUIRED_VALUE[f](ctx.invoice) is None or conf.get(f, 1.0) < config.CONFIDENCE_MIN)]
    desc = "; ".join(f"page {r['page']}: {r['description']}" for r in regions)
    if affected:
        return failed(f"Unreadable region(s) where required field(s) {', '.join(affected)} should be — {desc}",
                      ["UNREADABLE_REQUIRED_FIELD"], regions=regions, affected=affected)
    return warned(f"Unreadable region(s) reported, but every required field was read: {desc}", regions=regions)
