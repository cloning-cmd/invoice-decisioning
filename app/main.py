"""FastAPI app: API routes, the SSE stream and the static frontend, one process.

Run with a single worker (`uvicorn --workers 1`): live runs fan out through an
in-memory hub (app/services/events.py), which is not shared across processes.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, db, samples
from app.pipeline import runner
from app.services import actions, reports, runview
from app.services.events import TERMINAL_EVENTS, hub
from app.services.ocr import INSTALL_HINT, tesseract_version


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    hub.bind(asyncio.get_running_loop())
    interrupted = runner.mark_interrupted()
    if interrupted:
        print(f"[warn] marked {interrupted} unfinished run(s) from a previous server as interrupted")
    if tesseract_version() is None:
        print(f"[warn] {INSTALL_HINT}")
    yield


app = FastAPI(title="Invoice Decisioning", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------- health

@app.get("/api/health")
def health() -> dict[str, Any]:
    with db.session() as conn:
        counts = db.table_counts(conn)
    tess = tesseract_version()
    return {
        "ok": True,
        "tesseract": tess is not None,
        "tesseract_version": tess,
        "llm": config.LLM_CACHE_MODE,
        "llm_model": config.LLM_MODEL,
        # Whether a key is configured — never the key itself.
        "llm_key_configured": config.GEMINI_API_KEY is not None,
        "db_rows": sum(counts.values()),
        "counts": counts,
    }


# ---------------------------------------------------------------- starting runs

def _process(run_id: str, data: bytes, filename: str) -> None:
    """The pipeline, in a worker thread. Every transition goes to the hub."""
    def emit(event: str, payload: dict[str, Any]) -> None:
        if event in TERMINAL_EVENTS:
            with db.session() as conn:
                payload = {**payload, "card": runview.card(conn, run_id)}
        hub.publish(run_id, event, payload)

    try:
        runner.run_file(data, filename, emit=emit, run_id=run_id)
    except Exception as exc:
        # The runner itself broke (outside any stage, which would have been
        # recorded as a failed stage). Record it and close the stream, then
        # re-raise so the traceback reaches the server log.
        msg = f"pipeline crashed: {type(exc).__name__}: {exc}"
        with db.session() as conn:
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                         (runner.now_iso(), msg, run_id))
        if hub.is_live(run_id):
            hub.publish(run_id, "run_failed", {"run_id": run_id, "status": "failed", "decision": None,
                                               "error": msg, "card": None})
        raise


def _start(background: BackgroundTasks, data: bytes, filename: str) -> dict[str, str]:
    run_id = runner.create_run(filename)
    hub.open(run_id)            # before the task starts, so a fast subscriber misses nothing
    background.add_task(_process, run_id, data, filename)
    return {"run_id": run_id}


@app.post("/api/runs", status_code=202)
async def create_run(file: UploadFile, background: BackgroundTasks) -> dict[str, str]:
    data = await file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    return _start(background, data, file.filename or "upload")


class SampleRequest(BaseModel):
    sample: str


@app.post("/api/runs/from-sample", status_code=202)
def create_run_from_sample(body: SampleRequest, background: BackgroundTasks) -> dict[str, str]:
    sample = samples.BY_KEY.get(body.sample)
    if sample is None:
        raise HTTPException(404, f"unknown sample {body.sample!r}; known: {', '.join(samples.BY_KEY)}")
    path = samples.path_of(sample)
    if not path.exists():
        raise HTTPException(404, f"{sample.filename} has not been generated: run python -m tools.generate_samples")
    # Uploaded exactly as a user's file would be: same bytes, same filename.
    return _start(background, path.read_bytes(), sample.filename)


@app.get("/api/samples")
def list_samples() -> list[dict[str, Any]]:
    return [{"key": s.key, "label": s.label, "filename": s.filename, "group": s.group,
             "available": samples.path_of(s).exists()} for s in samples.SAMPLES]


# ---------------------------------------------------------------- reading runs

def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    with db.session() as conn:
        if db.query_one(conn, "SELECT id FROM runs WHERE id = ?", (run_id,)) is None:
            raise HTTPException(404, f"no run {run_id}")

    async def stream() -> AsyncIterator[str]:
        sub = hub.subscribe(run_id)
        if sub is None:
            # Finished (or from an earlier server): replay what is stored, then close.
            with db.session() as conn:
                for event, payload in runview.stored_events(conn, run_id):
                    yield _sse(event, payload)
            return
        history, queue = sub
        try:
            for event, payload in history:
                yield _sse(event, payload)
                if event in TERMINAL_EVENTS:
                    return
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=config.SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"     # an SSE comment: keeps proxies from closing the connection
                    continue
                yield _sse(event, payload)
                if event in TERMINAL_EVENTS:
                    return
        finally:
            hub.unsubscribe(run_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def list_runs(verdict: str | None = None, vendor: str | None = None, date_from: str | None = None,
              date_to: str | None = None, q: str | None = None, sort: str = "created_at", order: str = "desc",
              page: int = 1, page_size: int = config.RUNS_PAGE_SIZE_DEFAULT, queue: bool = False) -> dict[str, Any]:
    with db.session() as conn:
        if queue:
            rows = reports.exception_queue(conn)
            return {"total": len(rows), "page": 1, "page_size": len(rows), "rows": rows}
        try:
            return reports.list_runs(conn, verdict=verdict or None, vendor=vendor or None, date_from=date_from or None,
                                     date_to=date_to or None, q=q, sort=sort, order=order, page=page,
                                     page_size=page_size)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    with db.session() as conn:
        return reports.stats(conn)


@app.get("/api/reference/vendors")
def get_vendors() -> list[dict[str, Any]]:
    with db.session() as conn:
        return reports.vendors(conn)


@app.get("/api/reference/pos/{po_number}")
def get_po(po_number: str) -> dict[str, Any]:
    with db.session() as conn:
        po = reports.purchase_order(conn, po_number)
    if po is None:
        raise HTTPException(404, f"no purchase order {po_number}")
    return po


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with db.session() as conn:
        try:
            return runview.record(conn, run_id)
        except runview.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str) -> JSONResponse:
    """The complete decision record, including the verbatim model output."""
    with db.session() as conn:
        try:
            rec = runview.record(conn, run_id, include_raw=True)
        except runview.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    return JSONResponse(rec, headers={"Content-Disposition": f'attachment; filename="decision-{run_id}.json"'})


class ActionRequest(BaseModel):
    action: str
    actor: str
    note: str
    payload: dict[str, Any] | None = None


@app.post("/api/runs/{run_id}/actions")
def run_action(run_id: str, body: ActionRequest) -> dict[str, Any]:
    if hub.is_live(run_id):
        raise HTTPException(409, "this run is still in progress")
    try:
        return actions.perform(run_id, body.action, body.actor, body.note, body.payload)
    except actions.ActionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.get("/api/runs/{run_id}/page/{page}")
def get_page(run_id: str, page: int, variant: str = "original") -> FileResponse:
    with db.session() as conn:
        try:
            path, media = runview.page_image(conn, run_id, page, variant)
        except runview.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- frontend

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
