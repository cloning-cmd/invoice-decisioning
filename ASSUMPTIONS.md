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
- **`LLM_MODEL` defaults to `gemini-3.5-flash`, thinking level LOW, and both
  can be overridden in `.env`.** Measured on the build key (2026-11-16):
  `gemini-2.5-flash` answered 404 "no longer available to new users";
  `gemini-3.8-flash` took 212 s on the happy path and then timed out on a
  one-word prompt; `gemini-3.5-flash` read the same values in 8 s. Each cache
  entry records the model that produced it.
- **Every API call has a deadline (`LLM_TIMEOUT_SECONDS`, 180 s).** A hung
  call fails the extract stage with a readable reason and the run ends
  HOLD_FOR_REVIEW, rather than leaving the run open forever.
- **`GEMINI_API_KEY` in `.env` wins over a `GOOGLE_API_KEY` in the shell.** The
  key is passed to the client explicitly; the SDK's "using GOOGLE_API_KEY"
  warning is filtered because it is false in that case.
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

## API and live view

- **One server process, one worker.** Live runs fan out through an in-memory
  hub (`app/services/events.py`) with an `asyncio.Queue` per SSE connection.
  It is not shared across processes, so `run.sh` starts `--workers 1`. A
  prototype limitation, stated rather than hidden: scaling out would put the
  events on Redis or Postgres LISTEN/NOTIFY.
- **The pipeline runs as a Starlette `BackgroundTask` over a sync function,**
  which Starlette executes in its worker thread. The stages are blocking
  (SQLite, PyMuPDF, the Gemini SDK); running them on the event loop would
  freeze every open SSE stream. Events cross back to the loop with
  `call_soon_threadsafe`.
- **A refresh never shows an empty timeline.** A subscriber joining mid-run
  gets the run's full event history first; after the terminal event the hub
  forgets the run and the stream is rebuilt from SQLite, which is complete
  by then because the runner emits only after persisting.
- **Runs left `queued`/`running` by a stopped server are marked `failed`
  ("interrupted") at startup** rather than spinning forever in the UI.
- **Ingest registers the stored file on the run row immediately** (not at
  S10), so the document preview appears while extraction is still running.
- **`GET /api/samples`** (not in the brief's endpoint list) feeds the sample
  chips from the same registry `POST /api/runs/from-sample` uses
  (`app/samples.py`), so labels and filenames live in one place. A sample is
  uploaded exactly as a user's file would be: same bytes, same filename.
- **`static/js/ui.js`** (not in the brief's file list) holds the shared
  helpers: the element builder, integer-safe money formatting, icons, status
  vocabulary. Values are inserted as text nodes, never as HTML.
- **T1 matches also show a scored candidate ranking.** The explicit reference
  decides; the scores are corroboration for the match panel and change
  nothing in the decision.
- **"Variance" on the decision card is invoice total minus the PO's remaining
  amount** ("over" / "under"), in the PO currency only; across currencies it
  reads "currency differs" because no FX rates are held.
- **Page images for the viewer are rendered server-side at 150 DPI** (PNG
  for PDF pages, JPEG for photos), with EXIF orientation applied exactly as
  the pipeline read them, and cached next to the upload. Browsers cannot
  display TIFF, and the boxes must line up with what was read.

## Scanner path (S03) and the generated samples

- **Word boxes on scans are mapped back to the original page.** S03 records
  the deskew rotation and the upscale factor, and inverts both for every OCR
  word box, so the overlay is drawn on the image the clerk sees (Original).
  The Preprocessed toggle shows what tesseract read, without boxes.
- **The processed page is what OCR reads; the model sees the original.**
  Binarisation helps tesseract and loses information a vision model can use,
  so S04 sends the original page (downscaled) plus the OCR text.
- **Locating a field on the page tries its evidence, then each line of the
  evidence, then the value itself.** OCR reads a header strip label row
  first, then the value row, so "INVOICE NO. OE/2026/598" is never adjacent
  in reading order. Strings under 4 characters are not searched alone, and a
  match below `EVIDENCE_MATCH_MIN` draws no box rather than a wrong one.
- **The Edge 1 phone photo is rendered at 120 DPI of paper.** The spec's
  photo effects (warp, -1.8°, radial shadow, blur 0.8, noise 4, JPEG 72) on a
  200 DPI render still OCR at 0.885 legibility; 120 DPI measures 0.69,
  "around 0.6" as the brief describes and still above the 0.55 floor, so the
  run lands on AUTO_APPROVE once the clerk supplies the invoice number.
- **Deskew skips angles under 0.3°**, so the flat scans report "left as is"
  rather than rotating by noise.
- **Format samples are three extra clean invoices** (an image-only scanned
  PDF from Orbit on PO-2051, a PNG from Meridian on PO-2049, a 1-bit TIFF fax
  from Shreeji on PO-2052), each chosen to auto-approve so a correct decision
  proves the format path. They are separate invoices, not the happy path
  re-encoded, so running them after the happy path does not trip the
  duplicate controls. They appear as a second row of chips ("Formats").
- **Vendor layouts vary as the brief asks:** Nordic (V001) letterhead and a
  five-cell strip with right-aligned totals; Shreeji (V002) and Kaveri (V004)
  a two-column header with a details panel; Meridian (V003) prints no PO in
  the header, only in the notes; Apex (V006) a German layout with an address
  window, Pos./Article/HS code columns, European number format (6.980,00),
  tax-free export and IBAN/BIC.
- **Edge-case figures:** Edge 1 is ₹21,900 (₹18,000 + IGST ₹3,240 + freight
  ₹660); Edge 2 ₹1,92,000; Edge 3 ₹46,200 on PO-2052, dated 04/11/2026, two
  days after the paid INV-SH-7781; Edge 4 ₹2,98,400 (the full PO-2044 lines
  plus ₹10,480 freight) paid into Kotak …9043 / KKBK0008817; Edge 5
  ₹2,04,800 (100 boards at ₹1,700, revised from ₹1,650); Edge 6 EUR 6,980.00
  at 0% (export).
- **Tesseract was installed with winget** (UB-Mannheim 5.4.0, English) on
  the build machine. Without it S03 still preprocesses (so the before/after
  view works), warns, and extraction runs vision-only.

## Dashboard, actions and the edge-case screens

- **There is no login.** The header switch "Acting as AP Clerk / AP Manager"
  stands in for authentication, and the server enforces `OVERRIDE_ROLES` on
  every action (a clerk's attempt to release a BLOCK is a 403, not a hidden
  button). The choice is remembered per browser.
- **Overrides never rewrite the system decision.** `runs.decision` keeps what
  the controls said; the human resolution lives in `audit_log` and is shown
  next to it ("Rejected by Meera Krishnan, AP Manager"). Every action needs
  a note.
- **Supplying a field is the one action that re-decides.** The clerk's value
  replaces the model's reading at confidence 1.0, S05 onward re-runs on the
  stored extraction (the model is not called), and the run gets a new
  `decision_made` entry. Only required fields that were missing or read
  below the confidence floor can be supplied: this is exception handling,
  not a data-entry screen. Earlier supplied values are replayed too.
- **Schema v2 adds `vendors.email_domain`.** Edge 4 asks for the sender's
  domain mismatch to be flagged, which needs a known domain to compare with.
  The mismatch is reported in R12's message and evidence; on its own (same
  bank, odd domain) R12 only warns. Bank details decide.
- **The unreadable-region crop uses a box the model reports.** The schema's
  `unreadable_regions` gained `box_2d` (Gemini's native 0-1000
  [ymin, xmin, ymax, xmax]); prompt version 2026-11-16.2, all samples
  re-recorded. The model reports where it could not read, and the code
  draws the crop. Locating is reading, not deciding.
- **Edge 1's number is destroyed on the photo, not just blurred.** The crop
  shows the clerk where the field was; the demo narrative is that the clerk
  reads it from the paper copy in the inward register.
- **Edge 3's side-by-side compares header fields.** `invoice_history` keeps
  number, date, total, PO and status, not lines or GSTIN, so the diff shows
  what the archive holds and says so, rather than inventing an old GSTIN.
- **"Today" on the dashboard is the IST day** (`REPORTING_UTC_OFFSET_MINUTES`);
  each tile shows today with a 14-day sparkline. The exception queue is every
  run whose verdict needs a person (sign-off, hold, reject, block) and that
  has no human resolution since its latest decision, largest amount first.
- **Free-text search covers file name, invoice number, PO and run ID**, with
  SQL `LIKE` (wildcards in the input are escaped). Sorting is by a
  whitelisted column only.
- **`static/js/decision.js`** (not in the brief's file list) holds the
  decision card, its actions and the four edge-case panels, shared by the
  live run view and the run-detail screen.

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
