"""Full pipeline runs on generated fixtures, asserting the exact verdict and reasons.

Extraction is replayed from data/llm_cache in offline mode, so these tests are
deterministic, free, and never touch the network. A fixture with no recorded
extraction is skipped with the command that records it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from app import config, db
from app.pipeline import runner
from app.services import llm
from tools import generate_samples

TODAY = date(2026, 11, 16)

# filename -> (verdict, reason codes in order)
EXPECTED = {
    "HP_nordic_clean.pdf": ("AUTO_APPROVE", []),
}


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return {p.name: p for p in generate_samples.generate(tmp_path_factory.mktemp("samples"))}


def _require_recording(data: bytes, name: str) -> None:
    sha = hashlib.sha256(data).hexdigest()
    if not llm.cache_path(sha).exists():
        pytest.skip(f"no recorded extraction for {name} (sha {sha[:12]}); record it once with "
                    f"ANTHROPIC_API_KEY set: python -m app.cli run data/samples/{name}")


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


def test_same_file_again_after_approval_is_an_exact_duplicate(samples, fresh_db, monkeypatch):
    _run(samples, "HP_nordic_clean.pdf", fresh_db, monkeypatch)
    second = _run(samples, "HP_nordic_clean.pdf", fresh_db, monkeypatch)
    assert second.decision.verdict == "BLOCK" and second.decision.reason_codes == ["DUPLICATE_EXACT"]


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
