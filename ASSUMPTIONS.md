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
6. **[confirm] The happy path is ₹95,250, not ₹1,48,500.** Matrix branch 3
   sends anything above the ₹1,00,000 auto-approve ceiling to
   APPROVE_PENDING_SIGNOFF, yet the brief expects AUTO_APPROVE for ₹1,48,500.
   I kept the ceiling (a delegation-of-authority policy) and lowered the
   invoice (fixture data). ₹95,250 still sits inside PO-2041's ₹1,50,000
   remaining. To keep ₹1,48,500 instead, raise `AUTO_APPROVE_CEILING_MINOR`.
7. **`TEXT_COVERAGE_MIN` is 3.0 chars/sq.in, not 40.** Measured: the clean
   digital happy-path invoice scores 14.5 (an A4 page is 96.7 sq.in), so 40
   classed it as a scan. An image-only page scores 0. 3.0 separates them with
   margin on both sides.

## Extraction (S04)

- **Extraction runs on Google Gemini, not Claude (a deliberate deviation from
  the brief's stack table).** The key available for the build was a Gemini
  key. The switch is confined to `app/services/llm.py`: the extraction schema,
  the system prompt (including the required wording), the cache modes,
  account-number masking and validation are provider-neutral and unchanged.
  The architectural property the brief cares about holds exactly as before:
  the model reads, code decides, and the model is never shown the vendor
  master, the POs or the rules. Moving back is a rewrite of `_call_live`
  alone.
- **Guaranteed structure on Gemini:** function calling with mode `ANY` and a
  single allowed function whose `parameters_json_schema` is the extraction
  contract, the equivalent of a forced `tool_choice`. The result is then
  validated with jsonschema; on failure, one retry feeds the validator's own
  error messages back as the function response, and a second failure fails
  the stage. Temperature 0 and max 8,000 output tokens, as specified.
- **`LLM_MODEL` defaults to `gemini-2.5-flash` and can be overridden in
  `.env`.** Each cache entry records the model that produced it.
- **API errors become readable stage failures:** bad key (401/403), model
  unavailable (404), quota (429), server (5xx) and network errors each get a
  message a clerk can act on, never a stack trace.
- **The bank account number is masked before anything is stored.**
  `raw_llm_json` and the cache keep the model's output verbatim *except*
  `payment.account_number`, reduced to its last four (`••••••••••4471`). This
  is the one deviation from "verbatim", made because "never persist the full
  account number" is the stronger requirement. The reading artefact masks
  any 9+ digit run for the same reason.
- **Cache entries carry a `PROMPT_VERSION`.** In `cached` mode, an entry
  recorded under a different prompt or schema is re-read live rather than
  replayed against a contract it was not produced for.
- **Digital PDFs are rasterised at 150 DPI for the model** (A4 → 1754px,
  just above the 1568px target). 300 DPI is reserved for OCR.

## Normalise / self-check / match

- **The self-check formula includes shipping:** subtotal + tax − discount +
  shipping = grand total. Freight is printed outside the taxable value on
  most Indian invoices.
- **An unresolved DD/MM vs MM/DD date fails R09 (soft) as `DATE_AMBIGUOUS`.**
  The day-first reading is kept as the working value, but a human confirms
  it before the payment date is scheduled.
- **Vendor resolution runs twice:** once in S05 (only to choose the date
  locale) and once in S07 (the real match). Both are the same pure function.
- **Runs do not post to the ERP ledger.** Cumulative PO consumption reads
  `invoice_history` only. An approved run is not added to it, so Edge 2
  stays ₹42,000 over whatever ran before it.
- **"Same file already processed" (R04) means already *approved*:** present
  in `invoice_history`, or a prior run that was auto-approved, signed off or
  override-approved. Re-submitting a file that was rejected or held is a
  correction attempt, not a double payment. Running the happy path twice
  therefore BLOCKs the second run, which is correct, and a reason to start
  the demo from a fresh database.

## Rules and decision

- **The brief's table has 20 rules** (R01–R14 plus b/c/d variants).
- **A rule that crashes fails closed:** status `fail`, reason `RULE_ERROR`,
  at the rule's own severity, with the traceback in its evidence. Every
  other rule still runs.
- **R05b: a non-INR invoice with no PO always fails.** The ₹25,000 ceiling is
  an INR policy, and we hold no FX rates.
- **Non-INR invoices never auto-approve.** With no FX rate there is no way to
  compare them with the ₹1,00,000 limit, so a clean EUR invoice goes to
  APPROVE_PENDING_SIGNOFF (`FOREIGN_CURRENCY_APPROVER`).
- **R07b uses the tolerance band:** an invoice within tolerance of the full
  PO amount counts as a full invoice, not a partial.
- **R12 passes when no bank details are printed;** payment then goes to the
  master record, so there is nothing to redirect.
- **R14 needs both an unreadable region and a missing or low-confidence
  required field.** A region alone is a `warn`.
- **The reason codes for a hard verdict are the hard failures only** (see
  item 4). The explanation joins those rules' messages, ordered by
  `REASON_WEIGHTS`.

## Operations

- **`python -m app.cli run --replay <id>` is a dry run.** It re-decides with
  the current `config.py` and prints stored → replayed, but never rewrites
  the stored decision: history is evidence. Supplying a missing field
  (Phase 5) is the path that updates a run.
- **`INVOICE_AS_OF_DATE=YYYY-MM-DD` pins "today"** for the date rules, so a
  recorded demo gives the same verdict next quarter. Tests pin 2026-11-16.
- **Run IDs and timestamps are UTC.**
- **S10 writes a reading artefact** (`data/uploads/<sha>/reading.json`: text
  layer, word boxes, legibility) so a replay can start at S05 without
  re-reading the file.

## Fixture choices that make the edge cases emerge from data

- PO-2044 (Edge 4) and PO-2043 (Edge 1) allow partial invoicing, so
  under-billing is legitimate. PO-2047 (Edge 5) does not.
- Edge 3's original invoice INV-SH-7781 is booked against PO-2052 (₹1,45,000,
  partial allowed), not PO-2042. PO-2042 disallows partials, so a ₹46,200
  invoice against it would add a second, unrelated reason.
- No other history row shares a vendor and total with any sample invoice, so
  R04b fires only where intended.
