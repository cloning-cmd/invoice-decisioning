# Invoice Decisioning

Accounts-payable invoice decisioning prototype: one invoice file in, one
explained decision out. The model extracts; plain Python decides.

> Work in progress: Phase 0 (skeleton). The full README lands in Phase 6.

## Run

```bash
python -m pip install -r requirements.txt   # Python 3.11+
cp .env.example .env                         # set GEMINI_API_KEY
./run.sh                                     # http://127.0.0.1:8000
```

`./run.sh` creates the database, seeds fixtures (idempotent: run history is
kept), and serves the API and UI from one process. Check
`http://127.0.0.1:8000/api/health` before a demo.

Tesseract is optional at this stage. Without it, scans run vision-only.
