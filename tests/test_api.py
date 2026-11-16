"""The HTTP surface: starting runs, the SSE stream (live history and replay
after the fact), the run record, page images, and the event hub itself."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.pipeline import runner
from app.services import llm
from app.services.events import RunHub
from tools import generate_samples


@pytest.fixture
def client(fresh_db, monkeypatch, tmp_path):
    samples_dir = tmp_path / "samples"
    generate_samples.generate(samples_dir)
    monkeypatch.setattr(config, "DB_PATH", fresh_db)
    monkeypatch.setattr(config, "SAMPLES_DIR", samples_dir)
    monkeypatch.setattr(config, "LLM_CACHE_MODE", "offline")
    monkeypatch.setenv("INVOICE_AS_OF_DATE", "2026-11-16")
    with TestClient(app) as c:
        yield c


def _events(client, run_id):
    """Parse an SSE response into [(event, payload)]."""
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = "".join(resp.iter_text())
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def _require_hp_recording(client):
    data = (config.SAMPLES_DIR / "HP_nordic_clean.pdf").read_bytes()
    if not llm.cache_path(hashlib.sha256(data).hexdigest()).exists():
        pytest.skip("no recorded extraction for HP_nordic_clean.pdf")


def test_sample_run_streams_every_stage_and_the_decision(client):
    _require_hp_recording(client)
    resp = client.post("/api/runs/from-sample", json={"sample": "HP"})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    # The background task has finished, so this is the after-the-fact replay:
    # every stage settled, then the terminal event, then the stream closes.
    events = _events(client, run_id)
    stage_events = [p for e, p in events if e == "stage_finished"]
    assert [s["stage_key"] for s in stage_events] == [k for k, _, _ in runner.STAGES]
    assert events[-1][0] == "run_finished"
    card = events[-1][1]["card"]
    assert card["verdict"] == "AUTO_APPROVE"
    assert card["verdict_words"] == "Approved automatically"
    assert {f["key"]: f["value"] for f in card["figures"]}["total"] == "₹95,250.00"
    assert {f["key"]: f["value"] for f in card["figures"]}["po"] == "PO-2041"


def test_run_record_and_page_images(client):
    _require_hp_recording(client)
    run_id = client.post("/api/runs/from-sample", json={"sample": "HP"}).json()["run_id"]
    rec = client.get(f"/api/runs/{run_id}").json()
    assert rec["run"]["status"] == "completed" and rec["run"]["decision"] == "AUTO_APPROVE"
    assert len(rec["stages"]) == 10 and len(rec["rules"]) == 20
    assert sum(1 for f in rec["fields"] if f["bbox"]) > 10
    assert "raw_llm_json" not in rec and rec["has_raw_llm"] is True
    assert [a["action"] for a in rec["audit"]] == ["decision_made"]

    page = client.get(f"/api/runs/{run_id}/page/1")
    assert page.status_code == 200 and page.headers["content-type"] == "image/png"
    assert page.content.startswith(b"\x89PNG")
    assert client.get(f"/api/runs/{run_id}/page/2").status_code == 404
    assert client.get(f"/api/runs/{run_id}/page/1?variant=processed").status_code == 404   # digital PDF
    assert client.get(f"/api/runs/{run_id}/page/1?variant=thumbnail").status_code == 400


def test_unsupported_upload_fails_ingest_and_holds(client):
    resp = client.post("/api/runs", files={"file": ("notes.txt", b"just some text", "text/plain")})
    run_id = resp.json()["run_id"]
    events = _events(client, run_id)
    assert events[0][1]["stage_key"] == "ingest" and events[0][1]["status"] == "failed"
    assert events[-1][0] == "run_failed"
    assert events[-1][1]["card"]["reason_codes"] == ["STAGE_FAILED:ingest"]


def test_unknown_sample_and_unknown_run_are_404(client):
    assert client.post("/api/runs/from-sample", json={"sample": "EC99"}).status_code == 404
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_samples_listing_marks_generated_files(client):
    listing = {s["key"]: s for s in client.get("/api/samples").json()}
    assert listing["HP"]["available"] is True and listing["HP"]["label"] == "Happy path"


def test_no_api_key_in_any_response(client):
    bodies = [client.get("/api/health").text, client.get("/api/samples").text]
    assert "llm_key_configured" in bodies[0]
    if config.GEMINI_API_KEY:
        assert all(config.GEMINI_API_KEY not in b for b in bodies)


def test_interrupted_runs_are_marked_failed_at_startup(fresh_db):
    run_id = runner.create_run("half-done.pdf", fresh_db)
    assert runner.mark_interrupted(fresh_db) == 1
    with db.session(fresh_db) as conn:
        run = db.query_one(conn, "SELECT status, error FROM runs WHERE id = ?", (run_id,))
        stages = {r["status"] for r in db.query(conn, "SELECT status FROM run_stages WHERE run_id = ?", (run_id,))}
    assert run["status"] == "failed" and "interrupted" in run["error"]
    assert stages == {"skipped"}


def test_hub_gives_a_late_subscriber_the_full_history_then_live_events():
    async def scenario():
        hub = RunHub()
        hub.bind(asyncio.get_running_loop())
        hub.open("r1")
        worker = threading.Thread(target=lambda: [hub.publish("r1", "stage_started", {"n": 1}),
                                                  hub.publish("r1", "stage_finished", {"n": 1})])
        worker.start()
        worker.join()
        history, queue = hub.subscribe("r1")          # a refresh mid-run
        assert [e for e, _ in history] == ["stage_started", "stage_finished"]
        threading.Thread(target=lambda: hub.publish("r1", "run_finished", {})).start()
        event, _ = await asyncio.wait_for(queue.get(), timeout=2)
        assert event == "run_finished"
        assert hub.subscribe("r1") is None            # finished runs are served from the database

    asyncio.run(scenario())
