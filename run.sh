#!/usr/bin/env bash
# One command: init db, seed fixtures, generate samples if missing, serve.
set -euo pipefail
cd "$(dirname "$0")"

# Prefer a local venv, then python3, then python. Override with PYTHON=...
if [[ -z "${PYTHON:-}" ]]; then
  for candidate in .venv/bin/python .venv/Scripts/python.exe python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 || [[ -x "$candidate" ]]; then
      PYTHON="$candidate"; break
    fi
  done
fi
if [[ -z "${PYTHON:-}" ]]; then
  echo "error: no python found. Install Python 3.11+." >&2; exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "error: Python 3.11+ required, found $("$PYTHON" --version 2>&1) at $PYTHON" >&2; exit 1
fi
if ! "$PYTHON" -c 'import fastapi, pymupdf, cv2, google.genai, rapidfuzz, reportlab' 2>/dev/null; then
  echo "error: dependencies missing. Run: $PYTHON -m pip install -r requirements.txt" >&2; exit 1
fi

if [[ ! -f .env ]]; then
  echo "note: no .env found; copy .env.example to .env and set GEMINI_API_KEY."
fi

"$PYTHON" -m app.seed

if [[ -f tools/generate_samples.py ]] && [[ -z "$(ls -A data/samples | grep -v '^.gitkeep$' || true)" ]]; then
  echo "generating sample invoices..."
  "$PYTHON" -m tools.generate_samples
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "serving on http://$HOST:$PORT"
# Single worker on purpose: the SSE fan-out queue is in-memory (see ASSUMPTIONS.md).
exec "$PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1
