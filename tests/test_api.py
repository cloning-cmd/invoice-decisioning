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


# ---------------------------------------------------------------- Phase 4: dashboard, actions, export

def _require(client, name):
    data = (config.SAMPLES_DIR / name).read_bytes()
    path = llm.cache_path(hashlib.sha256(data).hexdigest())
    if not path.exists() or json.loads(path.read_text(encoding="utf-8")).get("prompt_version") != llm.PROMPT_VERSION:
        pytest.skip(f"no current recording for {name}")


def _start(client, key):
    return client.post("/api/runs/from-sample", json={"sample": key}).json()["run_id"]


def test_list_filters_search_sort_and_the_exception_queue(client):
    for name in ("HP_nordic_clean.pdf", "EC4_kaveri_redirect.pdf", "EC3_shreeji_reissue.pdf"):
        _require(client, name)
    hp, ec4, ec3 = _start(client, "HP"), _start(client, "EC4"), _start(client, "EC3")

    everything = client.get("/api/runs").json()
    assert everything["total"] == 3
    blocked = client.get("/api/runs", params={"verdict": "BLOCK"}).json()
    assert [r["id"] for r in blocked["rows"]] == [ec4]
    assert blocked["rows"][0]["vendor_name"] == "Kaveri Logistics Pvt Ltd"
    assert blocked["rows"][0]["total_display"] == "₹2,98,400.00"
    by_invoice = client.get("/api/runs", params={"q": "SH-7793"}).json()
    assert [r["id"] for r in by_invoice["rows"]] == [ec3]
    by_po = client.get("/api/runs", params={"q": "PO-2041"}).json()
    assert [r["id"] for r in by_po["rows"]] == [hp]
    by_amount = client.get("/api/runs", params={"sort": "amount", "order": "desc"}).json()
    assert [r["id"] for r in by_amount["rows"]] == [ec4, hp, ec3]
    assert client.get("/api/runs", params={"sort": "drop table"}).status_code == 400

    queue = client.get("/api/runs", params={"queue": "true"}).json()["rows"]
    assert [r["id"] for r in queue] == [ec4, ec3]           # needs a human, biggest first; HP is done

    stats = client.get("/api/stats").json()
    tiles = {t["key"]: t for t in stats["tiles"]}
    assert len(stats["days"]) == config.STATS_SERIES_DAYS and len(tiles["runs"]["series"]) == config.STATS_SERIES_DAYS
    assert tiles["runs"]["value"] == 3 and tiles["blocked"]["value"] == 1 and tiles["held"]["value"] == 1
    assert tiles["value_auto"]["display"] == "₹95,250"
    assert {r["code"]: r["count"] for r in stats["reasons"]} == {"BANK_DETAILS_CHANGED": 1, "POSSIBLE_DUPLICATE": 1}
    assert stats["queue_count"] == 2


def test_block_can_only_be_released_by_the_ap_manager_and_needs_a_note(client):
    _require(client, "EC4_kaveri_redirect.pdf")
    run_id = _start(client, "EC4")
    url = f"/api/runs/{run_id}/actions"
    clerk = client.post(url, json={"action": "override_approve", "actor": "ap_clerk", "note": "looks fine"})
    assert clerk.status_code == 403 and "AP Manager" in clerk.json()["detail"]
    no_note = client.post(url, json={"action": "override_approve", "actor": "ap_manager", "note": "  "})
    assert no_note.status_code == 400
    ok = client.post(url, json={"action": "override_reject", "actor": "ap_manager",
                                "note": "Called Kaveri on the master number: they never changed banks."})
    assert ok.status_code == 200

    rec = client.get(f"/api/runs/{run_id}").json()
    assert rec["run"]["decision"] == "BLOCK"                         # the system decision is never rewritten
    assert rec["card"]["resolution"]["action"] == "override_reject"
    assert rec["card"]["resolution"]["actor_name"] == config.OWNERS["ap_manager"]
    assert [(a["actor"], a["action"]) for a in rec["audit"]] == [("system", "decision_made"),
                                                                 ("ap_manager", "override_reject")]
    assert client.get("/api/runs", params={"queue": "true"}).json()["rows"] == []


def test_edge1_supplying_the_missing_number_lands_on_auto_approve(client):
    _require(client, "EC1_meridian_photo.jpg")
    run_id = _start(client, "EC1")
    rec = client.get(f"/api/runs/{run_id}").json()
    assert rec["run"]["decision"] == "REJECT" and "invoice_number" in rec["suppliable_fields"]
    region = rec["unreadable_regions"][0]
    assert region["page"] == 1 and len(region["box_2d"]) == 4

    url = f"/api/runs/{run_id}/actions"
    wrong = client.post(url, json={"action": "field_supplied", "actor": "ap_clerk", "note": "x",
                                   "payload": {"field": "totals.grand_total", "value": "1.00"}})
    assert wrong.status_code == 400                     # only fields the model could not read are suppliable
    resp = client.post(url, json={"action": "field_supplied", "actor": "ap_clerk",
                                  "note": "Read from the paper copy", "payload": {"field": "invoice_number",
                                                                                  "value": "MPL/26-27/0457"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "AUTO_APPROVE"

    rec = client.get(f"/api/runs/{run_id}").json()
    assert rec["run"]["decision"] == "AUTO_APPROVE"
    assert rec["supplied_fields"] == {"invoice_number": "MPL/26-27/0457"}
    assert [(a["actor"], a["action"]) for a in rec["audit"]] == [
        ("system", "decision_made"), ("ap_clerk", "field_supplied"), ("system", "decision_made")]
    assert rec["invoice"]["human_supplied"] == ["invoice_number"]


def test_export_is_the_complete_record_with_the_model_output(client):
    _require(client, "HP_nordic_clean.pdf")
    run_id = _start(client, "HP")
    resp = client.get(f"/api/runs/{run_id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == f'attachment; filename="decision-{run_id}.json"'
    body = resp.json()
    assert body["raw_llm_json"]["payload"]["invoice_number"]["value"] == "INV-NC-5531"
    assert body["card"]["verdict"] == "AUTO_APPROVE" and len(body["rules"]) == 20


def test_reference_endpoints(client):
    vendors = client.get("/api/reference/vendors").json()
    assert {v["id"] for v in vendors} >= {"V001", "V004"}
    po = client.get("/api/reference/pos/PO-2041").json()
    assert po["vendor_name"] == "Nordic Components Pvt Ltd" and po["allow_partial_invoicing"] is True
    assert [i["invoice_number"] for i in po["invoices"]] == ["INV-NC-5501", "INV-NC-5502"]
    assert client.get("/api/reference/pos/PO-9999").status_code == 404
