# Assumptions and judgement calls

Each entry: what I decided, and why. Entries marked **[confirm]** resolve a
place where the brief contradicts itself; they are my best reading and are
flagged for review.

## Data model

- **PO totals are the tax-inclusive authorised ceiling.** Invoice grand totals
  include GST, so the PO figure they are compared against must too. PO line
  items carry pre-tax unit prices and describe what was ordered; their sum
  plus GST is at or under `po_total`, with the remainder covering freight.
- **The vendor master has no country column.** Date-order disambiguation
  (DD/MM vs MM/DD) uses the vendor's default currency as a proxy
  (`DATE_ORDER_BY_CURRENCY`). Adding a column would break "exactly these tables".
- **`invoice_history` has no currency column.** `total_minor` is in the
  vendor's default currency (cents for V006 Apex, paise for everyone else).
- **`bank_ifsc` holds SWIFT/BIC for foreign vendors.** V006 stores
  `COBADEFFXXX`; `bank_account_last4` is the last four of the IBAN.
- **`audit_log.action` adds `field_supplied`.** Edge 1's "supply missing
  field" is neither an override nor a note. Folding it into `note_added` would
  hide a value change in the audit trail.
- **Fixture money is written in major units as strings** (`"600000.00"`), and
  converted to integer paise by `money.to_minor` at seed time. JSON is an edge;
  the database never sees a float.
- **The buyer is "Halden Precision Engineering Pvt Ltd", Bengaluru (GSTIN state
  29).** Karnataka vendors (V001) charge CGST+SGST; everyone else IGST.

## Environment

- **Python 3.12** in a local `.venv`; `run.sh` prefers `.venv`, then `python3`,
  then `python`, and refuses anything below 3.11.
- **Tesseract absence is a warning, not a failure.** `/api/health` reports it,
  and the startup log prints the install command.
- **`.env` is read by `config.py` itself** (a ten-line loader), so the CLI and
  tests see the same settings as `run.sh` without adding python-dotenv.
- **`run.sh` binds to 127.0.0.1.** Set `HOST=0.0.0.0` to expose it on a LAN.

## Spec conflicts I found while laying out the fixture numbers

1. **[confirm] R06 compares against PO remaining with `abs()`, which fails
   every legitimate partial invoice.** Edge 4 (₹2,98,400 against a ₹3,10,000
   PO, "every match rule passes") has an ₹11,600 gap against a ₹5,000
   tolerance, and Edge 1 has ₹600 against ₹500. My reading: on a PO with
   `allow_partial_invoicing = 1`, under-billing is expected, and R06 fails only
   when the invoice *exceeds* remaining by more than the tolerance. On a PO
   without partial invoicing, R06 uses `abs()` as written.
2. **[confirm] R07 as written also fires on Edge 5.** ₹0 prior + ₹2,04,800 >
   ₹2,00,000 + ₹4,000 tolerance, so R07 (hard) would turn Edge 5 into REJECT,
   but the brief expects HOLD_FOR_REVIEW. My reading: R07 is the *cumulative*
   check and applies only when prior approved or paid invoices exist on the
   PO. A single invoice over its PO is a price variance, so the soft rule R06
   handles it and a human sees it.
3. **[confirm] PO-2043 has "no GRN at all", yet Edge 1 must land on
   AUTO_APPROVE** after the clerk supplies the invoice number. A missing GRN
   fails R08 and would hold it. I gave PO-2043 a GRN (GRN-7150). The no-GRN
   case lives on PO-2050, a Kaveri freight-services PO.
4. **[confirm] Decision reasons for a hard verdict list only the hard
   failures.** Edge 2 expects exactly `PO_OVER_CONSUMPTION`, but R06 also fails
   (soft) on the same overage. Soft failures still appear in the rule table;
   the reason chips lead with what caused the verdict.
5. **[confirm] T2 (PO belongs to another vendor) still evaluates the quoted PO
   for status and currency.** Edge 6 expects `PO_STATUS_INVALID` and
   `CURRENCY_MISMATCH` alongside `PO_VENDOR_MISMATCH`, which needs R05d and R11
   to look at the quoted PO even though it was not matched. For the same
   reason R05 and R05b are not_applicable when a PO was quoted and found:
   R05c owns that failure.

## Fixture choices that make the edge cases emerge from data

- PO-2044 (Edge 4) and PO-2043 (Edge 1) allow partial invoicing, so
  under-billing is legitimate. PO-2047 (Edge 5) does not.
- Edge 3's original invoice INV-SH-7781 is booked against PO-2052 (₹1,45,000,
  partial allowed), not PO-2042. PO-2042 disallows partials, so a ₹46,200
  invoice against it would add a second, unrelated reason.
- No other history row shares a vendor and total with any sample invoice, so
  R04b fires only where intended.
