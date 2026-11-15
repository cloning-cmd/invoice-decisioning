"""S09 Decide: the decision matrix. A pure function of rule results + config.

No LLM here, ever. Evaluated top to bottom, first match wins:
  1. any hard rule failing  -> BLOCK if R12 (bank change) or R04 (exact duplicate), else REJECT
  2. any soft rule failing  -> HOLD_FOR_REVIEW
  3. clean, confident, above the delegated limit -> APPROVE_PENDING_SIGNOFF
  4. clean and confident    -> AUTO_APPROVE, with a scheduled payment date
"""
from __future__ import annotations

from datetime import timedelta

from app import config
from app.models import Decision, RuleResult, RunContext, StageResult, to_jsonable
from app.rules.definitions import run_confidence
from app.services.money import format_money

# Recommended next action per lead reason code: (action template, owner role).
# {po} is the PO in question. REJECT means "send back to the vendor"; BLOCK
# means "do not touch, escalate now" — the actions differ accordingly.
NEXT_ACTIONS: dict[str, tuple[str, str]] = {
    "BANK_DETAILS_CHANGED": ("Verify by phone on the number in the vendor master — do not reply to the invoice "
                             "email. Do not release payment until the vendor confirms the change in writing",
                             "ap_manager"),
    "DUPLICATE_EXACT": ("Do not pay. Confirm the original was settled, then close this as a duplicate", "ap_manager"),
    "RULE_ERROR": ("A control could not run; process this invoice manually and report the fault", "ap_manager"),
    "STAGE_FAILED": ("The pipeline could not finish; process this invoice manually", "ap_clerk"),
    "VENDOR_NOT_APPROVED": ("Return to Vendor Management: this supplier is not approved for payment",
                            "vendor_management"),
    "PO_VENDOR_MISMATCH": ("Request a corrected invoice from the vendor quoting their own PO; {po} belongs to "
                           "another supplier", "procurement"),
    "PO_OVER_CONSUMPTION": ("Route to Procurement: {po} is exhausted — raise a PO amendment or request a "
                            "credit note for the excess", "procurement"),
    "CURRENCY_MISMATCH": ("Request a corrected invoice from the vendor in the PO currency", "procurement"),
    "PO_STATUS_INVALID": ("Route to Procurement: {po} is closed or expired — reopen, extend or issue a new PO",
                          "procurement"),
    "PO_REQUIRED_FOR_AMOUNT": ("Route to Procurement for a PO, or request the vendor quote one", "procurement"),
    "REQUIRED_FIELD_MISSING": ("Read the missing field from the document and supply it; otherwise request a "
                               "corrected invoice from the vendor", "ap_clerk"),
    "POSSIBLE_DUPLICATE": ("Compare with the suspected original: approve if it is a legitimate re-issue, "
                           "reject as a duplicate otherwise", "ap_clerk"),
    "PO_AMOUNT_TOLERANCE": ("Route to Procurement to approve the variance on {po} or request a credit note",
                            "procurement"),
    "GRN_SHORT_RECEIPT": ("Confirm receipt with Stores before paying; pay only for what was received",
                          "stores"),
    "GRN_MISSING": ("Confirm with Stores that the goods on {po} were received before paying", "stores"),
    "ARITHMETIC_MISMATCH": ("Check the figures against the page; request a corrected invoice if the vendor's "
                            "arithmetic is wrong", "ap_clerk"),
    "PARTIAL_NOT_ALLOWED": ("Route to Procurement: {po} does not allow partial invoicing", "procurement"),
    "PO_NOT_FOUND": ("Choose the correct PO from the candidates, or route to Procurement", "ap_clerk"),
    "VENDOR_AMBIGUOUS": ("Confirm which vendor issued this invoice before it is matched", "ap_clerk"),
    "TAX_IMPLAUSIBLE": ("Check the GST rate against the HSN/SAC code; request a corrected invoice if wrong",
                        "ap_clerk"),
    "DATE_AMBIGUOUS": ("Confirm the invoice date from the document or with the vendor", "ap_clerk"),
    "DATE_INVALID": ("Confirm the invoice date with the vendor", "ap_clerk"),
    "UNREADABLE_REQUIRED_FIELD": ("Read the highlighted region and correct the field", "ap_clerk"),
    "LOW_CONFIDENCE": ("Check the low-confidence fields against the page and correct them", "ap_clerk"),
}

VERDICT_WORDS = {
    "AUTO_APPROVE": "Approved automatically",
    "APPROVE_PENDING_SIGNOFF": "Clean — awaiting sign-off",
    "HOLD_FOR_REVIEW": "Held for review",
    "REJECT": "Rejected — return to vendor",
    "BLOCK": "Blocked — do not pay",
}


def reason_weight(code: str) -> int:
    return config.REASON_WEIGHTS.get(code.split(":")[0], 0)


def _ordered(results: list[RuleResult]) -> tuple[list[str], list[RuleResult]]:
    """Reason codes and their rules, most important first."""
    pairs = [(code, r) for r in results for code in (r.reason_codes or [r.code])]
    pairs.sort(key=lambda p: reason_weight(p[0]), reverse=True)
    codes = list(dict.fromkeys(c for c, _ in pairs))
    rules = list(dict.fromkeys(r.code for _, r in pairs))
    by_code = {r.code: r for r in results}
    return codes, [by_code[c] for c in rules]


def _approver(total_minor: int, currency: str) -> str:
    if currency != "INR":
        return config.FOREIGN_CURRENCY_APPROVER  # no FX rates held, so no band applies
    for upper, role in config.APPROVAL_MATRIX:
        if upper is None or total_minor <= upper:
            return role
    return config.APPROVAL_MATRIX[-1][1]


def decide(ctx: RunContext) -> Decision:
    results, inv = ctx.rule_results, ctx.invoice
    fails = [r for r in results if r.status == "fail"]
    hard = [r for r in fails if r.severity == "hard"]
    soft = [r for r in fails if r.severity == "soft"]
    po = (ctx.po_match.po_number or ctx.po_match.quoted_po_number) if ctx.po_match else None

    def action_for(codes: list[str]) -> tuple[str, str]:
        template, role = NEXT_ACTIONS.get(codes[0].split(":")[0], ("Review manually", "ap_clerk"))
        return template.format(po=po or "the PO"), config.OWNERS[role]

    if hard:
        # The reasons for a hard verdict are the hard failures; soft ones stay
        # visible in the rule table but do not dilute the headline.
        codes, rules = _ordered(hard)
        blocked = any(r.code in config.BLOCK_RULE_CODES for r in hard)
        verdict = "BLOCK" if blocked else "REJECT"
        action, owner = action_for(codes)
        branch = ("1 · hard rule failed and it is a fraud/duplicate control (R12 or R04) → BLOCK" if blocked
                  else "1 · hard rule failed → REJECT")
        return Decision(verdict, codes, " ".join(r.message.rstrip(".") + "." for r in rules), action, owner, branch)

    if soft:
        codes, rules = _ordered(soft)
        action, owner = action_for(codes)
        return Decision("HOLD_FOR_REVIEW", codes, " ".join(r.message.rstrip(".") + "." for r in rules),
                        action, owner, "2 · soft rule failed → HOLD_FOR_REVIEW")

    conf = run_confidence(ctx)
    if conf is None or conf < config.CONFIDENCE_MIN:
        return Decision("HOLD_FOR_REVIEW", ["LOW_CONFIDENCE"],
                        f"All rules passed but extraction confidence {conf} is below {config.CONFIDENCE_MIN}.",
                        *action_for(["LOW_CONFIDENCE"]), "3/4 · confidence below floor → HOLD_FOR_REVIEW")

    cur = inv.currency or "INR"
    total = inv.grand_total_minor
    n = len(results)
    if cur != "INR" or total > config.AUTO_APPROVE_CEILING_MINOR:
        role = _approver(total, cur)
        why = (f"{format_money(total, cur)} is above the {format_money(config.AUTO_APPROVE_CEILING_MINOR)} "
               "delegated limit for unattended payment" if cur == "INR"
               else f"{cur} invoices always need sign-off (no FX conversion)")
        return Decision("APPROVE_PENDING_SIGNOFF", [],
                        f"All {n} controls passed at confidence {conf:.2f}, but {why}.",
                        f"Sign off for payment ({role.replace('_', ' ')} approval band)", config.OWNERS[role],
                        "3 · clean but above delegated authority → APPROVE_PENDING_SIGNOFF")

    vendor = ctx.ref.vendors[ctx.vendor_match.vendor_id]
    pay_on = inv.invoice_date + timedelta(days=vendor.payment_terms_days)
    return Decision("AUTO_APPROVE", [],
                    f"All {n} controls passed at confidence {conf:.2f}; {format_money(total, cur)} is within the "
                    f"{format_money(config.AUTO_APPROVE_CEILING_MINOR)} delegated limit.",
                    f"Release for payment on {pay_on.isoformat()} (invoice date + {vendor.payment_terms_days}-day "
                    "terms)", config.OWNERS["payment_run"], "4 · clean and confident → AUTO_APPROVE",
                    payment_date=pay_on)


def run(ctx: RunContext) -> StageResult:
    d = decide(ctx)
    ctx.decision = d
    detail = {**to_jsonable(d), "verdict_words": VERDICT_WORDS[d.verdict], "confidence": run_confidence(ctx)}
    reasons = f" [{', '.join(d.reason_codes)}]" if d.reason_codes else ""
    return StageResult("ok", f"{d.verdict}{reasons} — matrix branch {d.branch.split(' ·')[0]}", detail)
