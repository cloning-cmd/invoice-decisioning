"""Load vendor / PO / GRN / invoice-history fixtures into SQLite.

Idempotent: reference data is upserted from data/fixtures on every start, so
editing a fixture and restarting is enough. Pipeline history (runs, stages,
rules, audit) is never touched here.

Usage:  python -m app.seed
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app import config, db
from app.services.money import multiply_minor, to_minor


def _load(name: str, fixtures_dir: Path) -> list[dict[str, Any]]:
    with open(fixtures_dir / name, encoding="utf-8") as f:
        return json.load(f)


def _seed_vendors(conn: sqlite3.Connection, vendors: list[dict[str, Any]]) -> None:
    for v in vendors:
        conn.execute(
            """INSERT INTO vendors (id, legal_name, aliases_json, status, tax_id, bank_account_last4,
                                    bank_ifsc, payment_terms_days, default_currency, created_at, email_domain)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 legal_name=excluded.legal_name, aliases_json=excluded.aliases_json,
                 status=excluded.status, tax_id=excluded.tax_id,
                 bank_account_last4=excluded.bank_account_last4, bank_ifsc=excluded.bank_ifsc,
                 payment_terms_days=excluded.payment_terms_days,
                 default_currency=excluded.default_currency, email_domain=excluded.email_domain""",
            (v["id"], v["legal_name"], json.dumps(v.get("aliases", [])), v["status"], v.get("tax_id"),
             v.get("bank_account_last4"), v.get("bank_ifsc"), v["payment_terms_days"],
             v["default_currency"], v["created_at"], v.get("email_domain")),
        )


def _seed_purchase_orders(conn: sqlite3.Connection, pos: list[dict[str, Any]]) -> None:
    for po in pos:
        conn.execute(
            """INSERT INTO purchase_orders (id, po_number, vendor_id, currency, po_total_minor, status,
                                            allow_partial_invoicing, issue_date, expiry_date)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 po_number=excluded.po_number, vendor_id=excluded.vendor_id,
                 currency=excluded.currency, po_total_minor=excluded.po_total_minor,
                 status=excluded.status, allow_partial_invoicing=excluded.allow_partial_invoicing,
                 issue_date=excluded.issue_date, expiry_date=excluded.expiry_date""",
            (po["id"], po["po_number"], po["vendor_id"], po["currency"], to_minor(po["po_total"]),
             po["status"], po["allow_partial_invoicing"], po["issue_date"], po.get("expiry_date")),
        )
        # Line items have no natural key, so replace them wholesale for this PO.
        conn.execute("DELETE FROM po_line_items WHERE po_id = ?", (po["id"],))
        for line in po["lines"]:
            unit_minor = to_minor(line["unit_price"])
            qty = line["qty"]
            conn.execute(
                """INSERT INTO po_line_items (po_id, sku, description, qty, uom,
                                              unit_price_minor, line_total_minor)
                   VALUES (?,?,?,?,?,?,?)""",
                (po["id"], line["sku"], line["description"], qty, line["uom"], unit_minor,
                 multiply_minor(qty, unit_minor)),
            )


def _seed_goods_receipts(conn: sqlite3.Connection, grns: list[dict[str, Any]]) -> None:
    for g in grns:
        po = db.query_one(conn, "SELECT id FROM purchase_orders WHERE po_number = ?", (g["po_number"],))
        if po is None:
            raise ValueError(f"GRN {g['grn_number']} references unknown PO {g['po_number']}")
        conn.execute(
            """INSERT INTO goods_receipts (id, po_id, grn_number, received_date, lines_json)
               VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET po_id=excluded.po_id, grn_number=excluded.grn_number,
                 received_date=excluded.received_date, lines_json=excluded.lines_json""",
            (g["id"], po["id"], g["grn_number"], g["received_date"], json.dumps(g["lines"])),
        )


def _seed_invoice_history(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO invoice_history (id, vendor_id, invoice_number, invoice_date, po_number,
                                            total_minor, file_sha256, status)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET vendor_id=excluded.vendor_id,
                 invoice_number=excluded.invoice_number, invoice_date=excluded.invoice_date,
                 po_number=excluded.po_number, total_minor=excluded.total_minor,
                 file_sha256=excluded.file_sha256, status=excluded.status""",
            (r["id"], r["vendor_id"], r["invoice_number"], r["invoice_date"], r.get("po_number"),
             to_minor(r["total"]), r.get("file_sha256"), r["status"]),
        )


def seed(db_path: Path | None = None, fixtures_dir: Path | None = None) -> dict[str, int]:
    """Create the schema if needed and upsert every fixture. Returns table row counts."""
    fixtures = Path(fixtures_dir or config.FIXTURES_DIR)
    db.init_db(db_path)
    with db.session(db_path) as conn:
        _seed_vendors(conn, _load("vendors.json", fixtures))
        _seed_purchase_orders(conn, _load("purchase_orders.json", fixtures))
        _seed_goods_receipts(conn, _load("goods_receipts.json", fixtures))
        _seed_invoice_history(conn, _load("invoice_history.json", fixtures))
        return db.table_counts(conn)


def main() -> None:
    counts = seed()
    print("seeded " + ", ".join(f"{k}={v}" for k, v in counts.items()
                                if k in ("vendors", "purchase_orders", "po_line_items",
                                         "goods_receipts", "invoice_history")))
    print(f"history preserved: runs={counts['runs']}")


if __name__ == "__main__":
    main()
