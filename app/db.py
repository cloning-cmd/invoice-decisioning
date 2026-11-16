"""SQLite access: connection, schema DDL, migrations, small query helpers.

Plain sqlite3, no ORM, so every query is readable SQL. All money columns are
INTEGER minor units. Every statement is parameterised.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from app import config

SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS vendors (
    id                  TEXT PRIMARY KEY,
    legal_name          TEXT NOT NULL,
    aliases_json        TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL CHECK (status IN ('approved','on_hold','blocked','unknown')),
    tax_id              TEXT,
    bank_account_last4  TEXT,          -- master record; only the last four are ever stored
    bank_ifsc           TEXT,          -- IFSC for INR vendors, SWIFT/BIC for foreign vendors
    payment_terms_days  INTEGER NOT NULL,
    default_currency    TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id                      TEXT PRIMARY KEY,
    po_number               TEXT NOT NULL UNIQUE,
    vendor_id               TEXT NOT NULL REFERENCES vendors(id),
    currency                TEXT NOT NULL,
    po_total_minor          INTEGER NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('open','partially_invoiced','closed','cancelled')),
    allow_partial_invoicing INTEGER NOT NULL DEFAULT 0 CHECK (allow_partial_invoicing IN (0,1)),
    issue_date              TEXT NOT NULL,
    expiry_date             TEXT
);

CREATE TABLE IF NOT EXISTS po_line_items (
    id                INTEGER PRIMARY KEY,
    po_id             TEXT NOT NULL REFERENCES purchase_orders(id),
    sku               TEXT NOT NULL,
    description       TEXT NOT NULL,
    qty               REAL NOT NULL,
    uom               TEXT NOT NULL,
    unit_price_minor  INTEGER NOT NULL,
    line_total_minor  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS goods_receipts (
    id             TEXT PRIMARY KEY,
    po_id          TEXT NOT NULL REFERENCES purchase_orders(id),
    grn_number     TEXT NOT NULL,
    received_date  TEXT NOT NULL,
    lines_json     TEXT NOT NULL       -- [{"sku":"BRG-12","qty_received":40}]
);

CREATE TABLE IF NOT EXISTS invoice_history (
    id              TEXT PRIMARY KEY,
    vendor_id       TEXT NOT NULL REFERENCES vendors(id),
    invoice_number  TEXT NOT NULL,
    invoice_date    TEXT NOT NULL,
    po_number       TEXT,
    total_minor     INTEGER NOT NULL,
    file_sha256     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('paid','approved','rejected'))
);

CREATE TABLE IF NOT EXISTS runs (
    id                     TEXT PRIMARY KEY,   -- sortable: time-prefixed
    created_at             TEXT NOT NULL,
    finished_at            TEXT,
    source_filename        TEXT,
    stored_path            TEXT,
    mime                   TEXT,
    file_sha256            TEXT,
    page_count             INTEGER,
    is_scanned             INTEGER,
    status                 TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed')),
    decision               TEXT CHECK (decision IS NULL OR decision IN
                              ('AUTO_APPROVE','APPROVE_PENDING_SIGNOFF','HOLD_FOR_REVIEW','BLOCK','REJECT')),
    decision_reasons_json  TEXT,
    confidence             REAL,
    matched_po_number      TEXT,
    matched_vendor_id      TEXT,
    extracted_json         TEXT,
    raw_llm_json           TEXT,   -- verbatim model output: proves the decision was not model-authored
    duration_ms            INTEGER,
    error                  TEXT
);

CREATE TABLE IF NOT EXISTS run_stages (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    stage_key    TEXT NOT NULL CHECK (stage_key IN
                    ('ingest','textlayer','ocr','extract','normalise',
                     'selfcheck','match','rules','decide','persist')),
    stage_label  TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','running','ok','warn','failed','skipped')),
    started_at   TEXT,
    ended_at     TEXT,
    duration_ms  INTEGER,
    summary      TEXT,
    detail_json  TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS rule_results (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    rule_code      TEXT NOT NULL,
    rule_name      TEXT NOT NULL,
    category       TEXT NOT NULL CHECK (category IN
                      ('completeness','integrity','vendor','duplicate','po','risk','confidence')),
    severity       TEXT NOT NULL CHECK (severity IN ('hard','soft','info')),
    status         TEXT NOT NULL CHECK (status IN ('pass','warn','fail','not_applicable')),
    message        TEXT NOT NULL,
    evidence_json  TEXT
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    field_path     TEXT NOT NULL,      -- invoice_number | totals.grand_total | line_items[2].unit_price
    value_text     TEXT,
    value_norm     TEXT,
    confidence     REAL,
    evidence_text  TEXT,
    page           INTEGER,
    bbox_json      TEXT                -- [x0,y0,x1,y1] normalised 0-1, or NULL if not locatable
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    actor       TEXT NOT NULL CHECK (actor IN ('system','ap_clerk','ap_manager')),
    action      TEXT NOT NULL CHECK (action IN
                   ('decision_made','override_approve','override_reject','note_added','field_supplied')),
    from_value  TEXT,
    to_value    TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_runs_created_at     ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_runs_decision       ON runs(decision);
CREATE INDEX IF NOT EXISTS ix_rule_results_run    ON rule_results(run_id);
CREATE INDEX IF NOT EXISTS ix_run_stages_run_seq  ON run_stages(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_history_vendor_inv  ON invoice_history(vendor_id, invoice_number);
CREATE INDEX IF NOT EXISTS ix_history_sha         ON invoice_history(file_sha256);
"""

# v2: the vendor's known email domain (Edge 4 flags an invoice sent from a
# look-alike domain), and expression indexes so the dashboard can filter and
# sort on fields that live inside runs.extracted_json without a table scan.
SCHEMA_V2 = """
ALTER TABLE vendors ADD COLUMN email_domain TEXT;
CREATE INDEX IF NOT EXISTS ix_runs_total   ON runs(json_extract(extracted_json, '$.invoice.grand_total_minor'));
CREATE INDEX IF NOT EXISTS ix_runs_invno   ON runs(json_extract(extracted_json, '$.invoice.invoice_number'));
CREATE INDEX IF NOT EXISTS ix_runs_vendor  ON runs(matched_vendor_id);
CREATE INDEX IF NOT EXISTS ix_audit_run    ON audit_log(run_id, id);
"""

# Ordered migrations: index i upgrades user_version i -> i+1.
MIGRATIONS: list[str] = [SCHEMA_V1, SCHEMA_V2]

TABLES = ("vendors", "purchase_orders", "po_line_items", "goods_receipts", "invoice_history",
          "runs", "run_stages", "rule_results", "extracted_fields", "audit_log")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with rows addressable by column name and FKs enforced."""
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard read while a pipeline run is writing.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Connection that commits on success and rolls back on error."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> int:
    """Apply any pending migrations. Returns the resulting schema version."""
    with session(db_path) as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current, len(MIGRATIONS)):
            conn.executescript(MIGRATIONS[version])
            conn.execute(f"PRAGMA user_version = {version + 1}")  # int, not user input
        return conn.execute("PRAGMA user_version").fetchone()[0]


def query(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    # Table names come from the fixed TABLES tuple above, never from input.
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
