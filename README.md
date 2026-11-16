# Invoice Decisioning

An accounts-payable prototype: one invoice file in (PDF, PNG, JPEG or TIFF),
one explained decision out. **The model reads; plain Python decides.** Every
verdict comes from twenty named rules and a four-branch decision matrix, each
with a reason a clerk can act on, and every run is stored so it can be opened,
replayed and audited later.

Verdicts: `AUTO_APPROVE` · `APPROVE_PENDING_SIGNOFF` · `HOLD_FOR_REVIEW` ·
`REJECT` (send back to the vendor) · `BLOCK` (do not pay; escalate).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt             # Python 3.11+
cp .env.example .env                                  # set GEMINI_API_KEY
./run.sh                                              # http://127.0.0.1:8000
```

`./run.sh` creates and migrates the SQLite database, seeds the vendor master,
POs, goods receipts and invoice history (idempotent; run history is kept),
builds the sample invoices if any are missing, and serves the API and UI from
one process. Check `GET /api/health` before a demo: it reports the LLM mode,
the model, whether a key is configured, whether tesseract is installed, and
the row counts.

**Tesseract** (for the scanner path) is optional but recommended:
`brew install tesseract` · `sudo apt-get install tesseract-ocr` ·
`winget install UB-Mannheim.TesseractOCR`. Without it, scans still get a
decision (the model reads the page image), but there are no OCR word boxes or
legibility scores.

**No key, no network?** Every sample has a recorded extraction in
`data/llm_cache/`. With `LLM_CACHE_MODE=offline` the whole demo runs from those
recordings and never touches the network; a file without a recording fails its
extract stage with a clear message and is held, never guessed.

### Command line

```bash
python -m app.cli run data/samples/EC2_nordic_third_tranche.pdf   # stage trace, rule table, decision
python -m app.cli run --replay <run_id>      # re-decide a stored run with the current config.py, model not called
python -m pytest                             # 160+ tests: every rule both ways, matching, money, every sample end to end
```

## Architecture

```
upload ─► S01 ingest     magic-byte type check, size/page caps, sha256, content-addressed storage
          S02 text layer native PDF text + word boxes; coverage score decides "digital or scanned"
          S03 OCR        (scans only) deskew, shadow removal, threshold, tesseract PSM 6/4, legibility
          S04 extract    ONE model call: page images + machine text -> forced function call, schema-validated
          S05 normalise  dates by vendor locale, Decimal -> integer paise, currency, PO references
          S06 self-check the document's own arithmetic (lines, subtotal, tax, grand total)
          S07 match      vendor (exact/alias/GSTIN/fuzzy), PO (explicit, foreign, scored), GRN, duplicates
          S08 rules      20 pure functions over the context; all run, none short-circuit; a crash fails closed
          S09 decide     4-branch matrix -> verdict, ordered reason codes, next action, named owner
          S10 persist    run record, fields with page boxes, audit entry, a reading artefact for replay
```

Each stage writes its row to `run_stages` and emits an event; the browser
watches over SSE and a refresh mid-run replays the history first. The model
never sees the vendor master, the POs or the rules, so it cannot decide; its
verbatim output is stored in `runs.raw_llm_json` as evidence of that.
Every tunable number lives in `app/config.py` with its business reason.

Stack: FastAPI + Uvicorn, plain `sqlite3`, PyMuPDF, OpenCV, pytesseract,
RapidFuzz, reportlab (sample generator), Google Gemini for extraction (see
ASSUMPTIONS.md for why not Claude), static HTML + ES modules + Tailwind CDN,
no build step.

## The screens

- **Run** (`#/run`): drop a file or click a sample chip; watch ten stages
  settle in real time; the document on the left with a box on every field the
  model read (hover a field to light its box, click a box to find its field);
  the decision card with the verdict, reasons, next action, owner, key figures
  and the actions the current role may take.
- **Dashboard** (`#/dashboard`): today's KPIs with 14-day sparklines, the
  exception queue (largest amount first), decisions by reason code, and the
  full history with server-side filters, search, sort and paging.
- **Run detail** (`#/run/<id>`): the same view rebuilt from the database, plus
  every control grouped by category, the audit trail, and the complete
  decision record as JSON (including the model's verbatim output).

Opening a run from three days ago and seeing exactly what was read, which
controls passed, what the matrix decided and who overrode it, with what note,
is the difference between a demo and an auditable process.

## The samples

| Chip | File | What it proves | Verdict |
| --- | --- | --- | --- |
| Happy path | `HP_nordic_clean.pdf` | the boring case is boring | AUTO_APPROVE |
| Edge 1 | `EC1_meridian_photo.jpg` | "could not read" is not "wrong"; a clerk supplies the field and it re-decides | REJECT `REQUIRED_FIELD_MISSING:invoice_number` → AUTO_APPROVE |
| Edge 2 | `EC2_nordic_third_tranche.pdf` | the system holds state about the PO | REJECT `PO_OVER_CONSUMPTION` |
| Edge 3 | `EC3_shreeji_reissue.pdf` | duplicates are not a key lookup | HOLD `POSSIBLE_DUPLICATE` |
| Edge 4 | `EC4_kaveri_redirect.pdf` | a perfect match can still be fraud | BLOCK `BANK_DETAILS_CHANGED` |
| Edge 5 | `EC5_orbit_variance.pdf` | reasons stack, ordered by weight | HOLD `PO_AMOUNT_TOLERANCE`, `GRN_SHORT_RECEIPT` |
| Edge 6 | `EC6_apex_eur.pdf` | rules report independently | REJECT `PO_VENDOR_MISMATCH`, `CURRENCY_MISMATCH`, `PO_STATUS_INVALID` |
| Formats | `FMT_orbit_scan.pdf`, `FMT_meridian_scan.png`, `FMT_shreeji_fax.tif` | image-only PDF, PNG and 1-bit fax all read | AUTO_APPROVE |

All are produced by `python -m tools.generate_samples` (deterministic) and
every verdict above is asserted by `tests/test_pipeline_e2e.py`.
Demo order and talking points: `DEMO_SCRIPT.md`.

## Security hygiene

- The API key is read server-side from `.env` and never appears in a response
  or a static file (`/api/health` reports only whether one is configured).
- Full bank account numbers are never stored or logged: the model's output is
  masked to the last four digits before it is cached or persisted, and the
  stored text layer masks any run of nine or more digits.
- Uploads are stored by content hash; the original filename is metadata only.
- Every SQL statement is parameterised; sort columns come from a whitelist.
- File type is checked by magic bytes, never the extension; size and page
  count are capped.
- Overrides are enforced server-side by role, and every action needs a note.

## Known limitations

- **One process, one worker.** Live events fan out through an in-memory hub;
  scaling out would move them to Redis or Postgres LISTEN/NOTIFY.
- **No authentication.** The header's "Acting as" switch stands in for login;
  the server still enforces what each role may do.
- **No FX rates.** Non-INR invoices never auto-approve; they go to sign-off.
- **Runs do not post to the ERP.** Cumulative PO consumption reads the seeded
  invoice history; an approved run is not added to it.
- **The Gemini free tier allows a small number of requests per model per
  day.** The samples run from recordings; a new file needs quota.
- **English only.** Tesseract is installed with the English model; the
  extraction prompt is in English.

Every judgement call, and the reasoning behind the thresholds, is in
`ASSUMPTIONS.md`.
