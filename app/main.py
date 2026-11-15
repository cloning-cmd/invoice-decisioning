"""FastAPI app: API routes and the static frontend, served by one process.

Run with a single worker (`uvicorn --workers 1`): later phases keep an
in-memory SSE queue per run, which is not shared across processes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config, db
from app.services.ocr import INSTALL_HINT, tesseract_version


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    if tesseract_version() is None:
        print(f"[warn] {INSTALL_HINT}")
    yield


app = FastAPI(title="Invoice Decisioning", version="0.1.0", lifespan=lifespan)


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
        "llm_key_configured": config.ANTHROPIC_API_KEY is not None,
        "db_rows": sum(counts.values()),
        "counts": counts,
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
