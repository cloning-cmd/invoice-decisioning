# Demo script (5 minutes)

## Before recording

1. Start from an empty run history, so the dashboard shows only this demo and
   no earlier approval trips the duplicate controls:
   ```bash
   mv data/app.db data/app.db.bak 2>/dev/null; rm -f data/app.db-wal data/app.db-shm
   ```
2. In `.env`: `LLM_CACHE_MODE=offline` (every sample replays its recording:
   instant, free, identical every take, immune to venue wifi) and
   `INVOICE_AS_OF_DATE=2026-11-16` (the date rules see the same "today" the
   samples were written for).
3. `./run.sh`, open `http://127.0.0.1:8000`, browser at 1280 px or wider,
   "Acting as" set to **AP Clerk**.
4. Check the header badges read `LLM offline` and `OCR ready`.

## Minute by minute

**0:00 – 0:25 · What this is.**
"An accounts-payable clerk receives invoices as PDFs, scans and phone photos.
This system reads each one and decides: pay it, hold it, send it back, or
block it. One rule runs through the build: the model reads, code decides. The
model never sees the vendor master, the POs or the rules." Point at the badges:
LLM mode, OCR ready.

**0:25 – 1:00 · Happy path.** Click *Happy path*. Ten stages settle live.
Hover two rows in the Extract panel: the box lights up on the page. Click a
box on the total: the field row flashes. Card: *Approved automatically*, pay
on 2 December (invoice date + 30-day terms), owner the payment run. Open
*Decide*: branch 4 of the matrix fired, with the live thresholds.

**1:00 – 1:55 · Edge 1, the phone photo.** Click *Edge 1 · Phone photo*.
OCR ran: open the panel, legibility 0.69 with PSM 6, deskew and shadow
removal applied. Toggle *Preprocessed* to show what tesseract read. Verdict
*Rejected*, reason `REQUIRED_FIELD_MISSING:invoice_number`. "The model did
not guess: it returned null and said where it could not read." Show the crop.
Click *Supply invoice_number*, type `MPL/26-27/0457`, note "Read from the
paper copy in the inward register". It re-decides from Normalise onward
without calling the model: *Approved automatically*. "A designed path, not a
fallback."

**1:55 – 2:30 · Edge 2, the split invoice.** Click *Edge 2 · Split
invoice*. ₹1,92,000 alone is well under a ₹6,00,000 PO. The consumption bar:
two approved invoices, this one, and ₹42,000 in red past the ceiling. The
message names INV-NC-5501 and INV-NC-5502. *Rejected*, routed to Procurement.
"It holds state about the PO; each invoice is not an isolated document."

**2:30 – 3:00 · Edge 3, the re-issue.** Click *Edge 3 · Near-duplicate*.
Hash and number checks pass; the side-by-side shows same vendor, same
amount, same PO, two days apart, new number. *Held for review*, on purpose:
"Re-issues are sometimes legitimate. Auto-rejecting them is a vendor
relationship problem, so this is where we put a person." One click either way.

**3:00 – 3:45 · Edge 4, the bank change.** Click *Edge 4 · Bank change*.
Everything matches: vendor, PO, GRN, dates, arithmetic. One thing does not:
the account is …9043 at Kotak, the master says …6652 at SBI, and the email
comes from `kaveri-logistics.co`, not `.in`. *Blocked, do not pay*, and not
*Rejected*: "you don't send this back to the vendor, because the vendor may
not be who sent it." Show *Locked* for the clerk. Switch to **AP Manager**:
Reject, note "Called Kaveri on the master number; they have not changed
banks."

**3:45 – 4:20 · Dashboard and audit.** Open *Dashboard*. The queue is
sorted by amount, the order an AP lead works in. Reason-code bars. Type
`KLPL` in search (server-side, so it works at 10,000 rows). Open the Edge 4
run: controls grouped by category, the audit trail with the system decision
and the manager's rejection. Download the decision record: `raw_llm_json`
holds the model's reading and nothing else.

**4:20 – 4:50 · Change a threshold, replay.** In `app/config.py` set
`AUTO_APPROVE_CEILING_MINOR = 50_000_00`, then
`python -m app.cli run --replay <happy-path run id>`: stored AUTO_APPROVE,
replayed APPROVE_PENDING_SIGNOFF, model not called, history not rewritten.
(Or set `PO_TOLERANCE_PCT = 0.03` and replay Edge 5: `PO_AMOUNT_TOLERANCE`
drops out and only the short receipt holds it.) Put the value back.

**4:50 – 5:00 · Close.** "160 tests cover every rule both ways and every
sample end to end. Known limits and every judgement call are in the README
and ASSUMPTIONS."

## If something goes wrong on camera

- A chip says the file is missing: run `python -m tools.generate_samples`.
- A stage fails: the run is held with the reason on screen. That is the
  designed behaviour; say so and open the stage panel.
- Edge 2 or Edge 4 show an unexpected duplicate: the database was not reset.
  Repeat step 1.
