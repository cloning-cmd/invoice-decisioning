"""Full pipeline runs on generated fixtures, asserting the exact verdict and reasons.

Extraction is replayed from data/llm_cache in offline mode, so these tests are
deterministic, free, and never touch the network. A fixture with no recorded
extraction is skipped with the command that records it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app import config, db
from app.pipeline import runner
from app.services import llm
from tools import generate_samples

TODAY = date(2026, 11, 16)

# filename -> (verdict, reason codes in order)
EXPECTED = {
    "HP_nordic_clean.pdf": ("AUTO_APPROVE", []),
    "EC1_meridian_photo.jpg": ("REJECT", ["REQUIRED_FIELD_MISSING:invoice_number"]),
    "EC2_nordic_third_tranche.pdf": ("REJECT", ["PO_OVER_CONSUMPTION"]),
    "EC3_shreeji_reissue.pdf": ("HOLD_FOR_REVIEW", ["POSSIBLE_DUPLICATE"]),
    "EC4_kaveri_redirect.pdf": ("BLOCK", ["BANK_DETAILS_CHANGED"]),
    "EC5_orbit_variance.pdf": ("HOLD_FOR_REVIEW", ["PO_AMOUNT_TOLERANCE", "GRN_SHORT_RECEIPT"]),
    "EC6_apex_eur.pdf": ("REJECT", ["PO_VENDOR_MISMATCH", "CURRENCY_MISMATCH", "PO_STATUS_INVALID"]),
    # Input formats: a clean invoice must survive being scanned, faxed or saved as an image.
    "FMT_orbit_scan.pdf": ("AUTO_APPROVE", []),
    "FMT_meridian_scan.png": ("AUTO_APPROVE", []),
    "FMT_shreeji_fax.tif": ("AUTO_APPROVE", []),
}


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return {p.name: p for p in generate_samples.generate(tmp_path_factory.mktemp("samples"))}


def _require_recording(data: bytes, name: str) -> None:
    sha = hashlib.sha256(data).hexdigest()
    path = llm.cache_path(sha)
    if not path.exists():
        pytest.skip(f"no recorded extraction for {name} (sha {sha[:12]}); record it once with "
                    f"GEMINI_API_KEY set: python -m app.cli run data/samples/{name}")
    recorded = json.loads(path.read_text(encoding="utf-8")).get("prompt_version")
    if recorded != llm.PROMPT_VERSION:
        pytest.skip(f"the recording for {name} was made under prompt {recorded}, not {llm.PROMPT_VERSION}; "
                    f"re-record it: python -m app.cli run data/samples/{name}")


def _run(samples, name, db_path, monkeypatch):
    data = samples[name].read_bytes()
    _require_recording(data, name)
    monkeypatch.setattr(config, "LLM_CACHE_MODE", "offline")
    return runner.run_file(data, name, db_path=db_path, today=TODAY)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_verdict(name, samples, fresh_db, monkeypatch):
    ctx = _run(samples, name, fresh_db, monkeypatch)
    verdict, reasons = EXPECTED[name]
    assert ctx.failed_stage is None, ctx.failure_error
    assert ctx.decision.verdict == verdict
    assert ctx.decision.reason_codes == reasons


def test_happy_path_details(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "HP_nordic_clean.pdf", fresh_db, monkeypatch)
    from app.rules.definitions import run_confidence
    assert [r.code for r in ctx.rule_results if r.status == "fail"] == []
    assert run_confidence(ctx) > 0.95
    assert ctx.decision.payment_date == date(2026, 12, 2)
    assert ctx.po_match.tier == "T1" and ctx.consumption.remaining_minor == 15000000

    with db.session(fresh_db) as conn:
        run = db.query_one(conn, "SELECT * FROM runs WHERE id = ?", (ctx.run_id,))
        stages = db.query(conn, "SELECT stage_key, status FROM run_stages WHERE run_id = ? ORDER BY seq",
                          (ctx.run_id,))
        n_rules = db.query_one(conn, "SELECT COUNT(*) n FROM rule_results WHERE run_id = ?", (ctx.run_id,))["n"]
        boxes = db.query_one(conn, "SELECT COUNT(*) n FROM extracted_fields WHERE run_id = ? AND bbox_json "
                                   "IS NOT NULL", (ctx.run_id,))["n"]
        audit = db.query(conn, "SELECT actor, action, to_value FROM audit_log WHERE run_id = ?", (ctx.run_id,))
    assert run["status"] == "completed" and run["decision"] == "AUTO_APPROVE"
    assert json.loads(run["raw_llm_json"])["payload"]["document_type"] == "tax_invoice"
    assert [s["status"] for s in stages] == ["ok", "ok", "skipped", "ok", "ok", "ok", "ok", "ok", "ok", "ok"]
    assert n_rules == 20 and boxes > 10
    assert audit == [{"actor": "system", "action": "decision_made", "to_value": "AUTO_APPROVE"}]
    # The full account number never reaches the database.
    assert "50200012344471" not in run["raw_llm_json"]


def test_replay_with_a_lower_ceiling_changes_the_verdict_without_rewriting_history(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "HP_nordic_clean.pdf", fresh_db, monkeypatch)
    monkeypatch.setattr(config, "AUTO_APPROVE_CEILING_MINOR", 50_000_00)       # Rs 50,000
    replayed = runner.replay(ctx.run_id, db_path=fresh_db, today=TODAY)
    assert replayed.decision.verdict == "APPROVE_PENDING_SIGNOFF"
    with db.session(fresh_db) as conn:
        assert db.query_one(conn, "SELECT decision FROM runs WHERE id = ?", (ctx.run_id,))["decision"] == "AUTO_APPROVE"


def test_replay_with_a_wider_tolerance_clears_the_variance_on_edge5(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "EC5_orbit_variance.pdf", fresh_db, monkeypatch)
    assert ctx.decision.reason_codes == ["PO_AMOUNT_TOLERANCE", "GRN_SHORT_RECEIPT"]
    monkeypatch.setattr(config, "PO_TOLERANCE_PCT", 0.03)        # 3% of Rs 2,00,000 = Rs 6,000, capped at Rs 5,000
    replayed = runner.replay(ctx.run_id, db_path=fresh_db, today=TODAY)
    assert replayed.decision.verdict == "HOLD_FOR_REVIEW"         # the short receipt still needs Stores
    assert replayed.decision.reason_codes == ["GRN_SHORT_RECEIPT"]


def test_same_file_again_after_approval_is_an_exact_duplicate(samples, fresh_db, monkeypatch):
    _run(samples, "HP_nordic_clean.pdf", fresh_db, monkeypatch)
    second = _run(samples, "HP_nordic_clean.pdf", fresh_db, monkeypatch)
    assert second.decision.verdict == "BLOCK" and second.decision.reason_codes == ["DUPLICATE_EXACT"]


def _rule(ctx, code):
    return next(r for r in ctx.rule_results if r.code == code)


def test_edge2_names_the_prior_invoices_and_the_overage(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "EC2_nordic_third_tranche.pdf", fresh_db, monkeypatch)
    msg = _rule(ctx, "R07_PO_OVER_CONSUMPTION").message
    assert "INV-NC-5501" in msg and "INV-NC-5502" in msg and "₹42,000.00 over" in msg
    assert ctx.consumption.consumed_minor == 4_50_000_00 and ctx.consumption.remaining_minor == 1_50_000_00


def test_edge3_points_at_the_suspected_original(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "EC3_shreeji_reissue.pdf", fresh_db, monkeypatch)
    assert _rule(ctx, "R04_DUPLICATE_EXACT").status == "pass"
    assert [d["invoice_number"] for d in ctx.duplicates.near] == ["INV-SH-7781"]


def test_edge4_blocks_with_phone_verification_and_both_accounts(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "EC4_kaveri_redirect.pdf", fresh_db, monkeypatch)
    r12 = _rule(ctx, "R12_BANK_DETAILS_UNCHANGED")
    assert "9043" in r12.message and "6652" in r12.message
    assert ctx.decision.next_action.lower().startswith("verify by phone")
    assert "ap_manager" in config.OVERRIDE_ROLES["BLOCK"] and "ap_clerk" not in config.OVERRIDE_ROLES["BLOCK"]
    # every other control passed: the bank change alone stops it
    assert [r.code for r in ctx.rule_results if r.status == "fail"] == ["R12_BANK_DETAILS_UNCHANGED"]


def test_edge6_reports_all_three_hard_failures_independently(samples, fresh_db, monkeypatch):
    ctx = _run(samples, "EC6_apex_eur.pdf", fresh_db, monkeypatch)
    assert ctx.po_match.tier == "T2" and ctx.invoice.currency == "EUR"
    assert len(ctx.rule_results) == 20     # no short-circuit: every rule ran


# ---------------------------------------------------------------- the scanner path

def _needs_tesseract():
    from app.services.ocr import tesseract_version
    if tesseract_version() is None:
        pytest.skip("tesseract is not installed")


def test_scanned_pdf_is_image_only(samples):
    import pymupdf
    page = pymupdf.open(samples["FMT_orbit_scan.pdf"])[0]
    chars = len("".join(page.get_text("text").split()))
    assert chars / ((page.rect.width / 72) * (page.rect.height / 72)) < config.TEXT_COVERAGE_MIN


def test_scan_goes_through_ocr_and_boxes_land_on_the_printed_words(samples, fresh_db, monkeypatch):
    _needs_tesseract()
    import pymupdf
    ctx = _run(samples, "FMT_orbit_scan.pdf", fresh_db, monkeypatch)
    assert ctx.is_scanned and not ctx.ocr_unavailable and ctx.legibility_by_page[1] > config.OCR_LEGIBILITY_MIN
    assert ctx.word_boxes and all(w["source"] == "ocr" for w in ctx.word_boxes)
    assert Path(ctx.processed_page_paths[0]).name == "p1_proc.png" and Path(ctx.processed_page_paths[0]).exists()
    # Where the invoice number really is, from the clean digital render of the same document.
    clean = pymupdf.open(stream=generate_samples.as_clean_pdf(generate_samples.fmt_orbit_scan()), filetype="pdf")[0]
    r = clean.search_for("OE/2026/598")[0]
    truth = (r.x0 / clean.rect.width, r.y0 / clean.rect.height, r.x1 / clean.rect.width, r.y1 / clean.rect.height)
    with db.session(fresh_db) as conn:
        row = db.query_one(conn, "SELECT bbox_json FROM extracted_fields WHERE run_id = ? AND field_path = "
                                 "'invoice_number'", (ctx.run_id,))
    box = json.loads(row["bbox_json"])
    centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    assert truth[0] - 0.02 <= centre[0] <= truth[2] + 0.02 and truth[1] - 0.02 <= centre[1] <= truth[3] + 0.02


def test_phone_photo_is_deskewed_and_reads_degraded_but_legible(samples, fresh_db, monkeypatch):
    _needs_tesseract()
    ctx = _run(samples, "EC1_meridian_photo.jpg", fresh_db, monkeypatch)
    with db.session(fresh_db) as conn:
        detail = json.loads(db.query_one(conn, "SELECT detail_json FROM run_stages WHERE run_id = ? AND "
                                               "stage_key = 'ocr'", (ctx.run_id,))["detail_json"])
    steps = {s["step"]: s["applied"] for s in detail["steps"]}
    assert steps["deskew"] and steps["shadow removal"] and steps["threshold"]
    assert config.OCR_LEGIBILITY_MIN < ctx.legibility_by_page[1] < 0.8
    assert ctx.extraction["unreadable_regions"], "the model should report where the number was lost"


# ---------------------------------------------------------------- honest degradation (no recording needed)

def test_offline_without_recording_fails_extract_and_holds(samples, fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_CACHE_MODE", "offline")
    monkeypatch.setattr(config, "LLM_CACHE_DIR", tmp_path / "empty_cache")
    ctx = runner.run_file(samples["HP_nordic_clean.pdf"].read_bytes(), "HP_nordic_clean.pdf",
                          db_path=fresh_db, today=TODAY)
    assert ctx.decision.verdict == "HOLD_FOR_REVIEW"
    assert ctx.decision.reason_codes == ["STAGE_FAILED:extract"]
    with db.session(fresh_db) as conn:
        run = db.query_one(conn, "SELECT status, error FROM runs WHERE id = ?", (ctx.run_id,))
        stages = {s["stage_key"]: s["status"] for s in
                  db.query(conn, "SELECT stage_key, status FROM run_stages WHERE run_id = ?", (ctx.run_id,))}
    assert run["status"] == "failed" and "no network call" in run["error"]
    assert stages["extract"] == "failed" and stages["persist"] == "ok"
    assert all(stages[k] == "skipped" for k in ("normalise", "selfcheck", "match", "rules", "decide"))


def test_unsupported_file_type_fails_ingest(fresh_db):
    ctx = runner.run_file(b"PK\x03\x04 this is a zip", "invoice.pdf", db_path=fresh_db, today=TODAY)
    assert ctx.decision.reason_codes == ["STAGE_FAILED:ingest"]
    assert "unsupported file type" in ctx.failure_error
