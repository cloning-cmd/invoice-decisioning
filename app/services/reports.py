"""Dashboard queries: the filtered run list, the exception queue, KPIs and the
14-day series, reason-code counts, and reference lookups.

Filtering, sorting and paging happen in SQL, so the dashboard behaves the same
with 10,000 runs as with ten. Fields that live inside runs.extracted_json are
read with json_extract (indexed in schema v2).
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app import config, db
from app.services.money import format_money

_INV = "json_extract(r.extracted_json, '$.invoice.{}')"
INVOICE_NUMBER = _INV.format("invoice_number")
TOTAL = _INV.format("grand_total_minor")
CURRENCY = _INV.format("currency")

# The latest human resolution after the latest decision: a field supplied by a
# clerk re-decides the run, so an override before that no longer applies.
RESOLUTION = """(SELECT a.action || '|' || a.actor || '|' || a.created_at FROM audit_log a
                  WHERE a.run_id = r.id AND a.action IN ('override_approve', 'override_reject')
                    AND a.id > COALESCE((SELECT MAX(b.id) FROM audit_log b
                                         WHERE b.run_id = r.id AND b.action = 'decision_made'), 0)
                  ORDER BY a.id DESC LIMIT 1)"""

NEEDS_HUMAN = ("APPROVE_PENDING_SIGNOFF", "HOLD_FOR_REVIEW", "REJECT", "BLOCK")

SORTS = {"created_at": "r.created_at", "amount": TOTAL, "duration": "r.duration_ms", "confidence": "r.confidence",
         "verdict": "r.decision"}

VERDICTS = ("AUTO_APPROVE", "APPROVE_PENDING_SIGNOFF", "HOLD_FOR_REVIEW", "REJECT", "BLOCK")


def local_day(column: str = "r.created_at") -> str:
    return f"date({column}, '+{config.REPORTING_UTC_OFFSET_MINUTES} minutes')"


def today_local() -> date:
    return (datetime.now(timezone.utc) + timedelta(minutes=config.REPORTING_UTC_OFFSET_MINUTES)).date()


def _row(r: dict[str, Any]) -> dict[str, Any]:
    res = r.pop("resolution")
    action, actor, at = res.split("|", 2) if res else (None, None, None)
    r["resolution"] = {"action": action, "actor": actor, "at": at} if action else None
    r["reasons"] = json.loads(r.pop("decision_reasons_json") or "[]")
    cur = r.get("currency") or "INR"
    r["total_display"] = format_money(r["total_minor"], cur) if r.get("total_minor") is not None else None
    return r


def _select(where: str) -> str:
    return f"""SELECT r.id, r.created_at, r.source_filename, r.mime, r.status, r.decision, r.decision_reasons_json,
                      r.confidence, r.matched_po_number, r.matched_vendor_id, v.legal_name AS vendor_name,
                      r.duration_ms, r.error, {INVOICE_NUMBER} AS invoice_number, {TOTAL} AS total_minor,
                      {CURRENCY} AS currency, {RESOLUTION} AS resolution
               FROM runs r LEFT JOIN vendors v ON v.id = r.matched_vendor_id
               WHERE {where}"""


def list_runs(conn, *, verdict: str | None = None, vendor: str | None = None, date_from: str | None = None,
              date_to: str | None = None, q: str | None = None, sort: str = "created_at", order: str = "desc",
              page: int = 1, page_size: int = config.RUNS_PAGE_SIZE_DEFAULT) -> dict[str, Any]:
    where, params = ["1 = 1"], []
    if verdict:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        where.append("r.decision = ?")
        params.append(verdict)
    if vendor:
        where.append("r.matched_vendor_id = ?")
        params.append(vendor)
    if date_from:
        where.append(f"{local_day()} >= ?")
        params.append(date.fromisoformat(date_from).isoformat())
    if date_to:
        where.append(f"{local_day()} <= ?")
        params.append(date.fromisoformat(date_to).isoformat())
    if q and q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(f"(r.source_filename LIKE ? ESCAPE '\\' OR {INVOICE_NUMBER} LIKE ? ESCAPE '\\' "
                     "OR r.matched_po_number LIKE ? ESCAPE '\\' OR r.id LIKE ? ESCAPE '\\')")
        params += [like] * 4
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {', '.join(SORTS)}")
    direction = "ASC" if order == "asc" else "DESC"
    page_size = max(1, min(page_size, config.RUNS_PAGE_SIZE_MAX))
    page = max(1, page)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM runs r WHERE {clause}", params).fetchone()[0]
    # Column and direction come from the whitelists above, never from input.
    rows = db.query(conn, _select(clause) + f" ORDER BY {SORTS[sort]} {direction} NULLS LAST, r.id DESC "
                                            "LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size])
    return {"total": total, "page": page, "page_size": page_size, "rows": [_row(r) for r in rows]}


def exception_queue(conn, limit: int = config.RUNS_PAGE_SIZE_DEFAULT) -> list[dict[str, Any]]:
    """Decided runs that still need a person, largest amount first: the order an AP lead works in."""
    marks = ",".join("?" * len(NEEDS_HUMAN))
    rows = db.query(conn, _select(f"r.decision IN ({marks}) AND {RESOLUTION} IS NULL")
                    + f" ORDER BY {TOTAL} DESC NULLS LAST, r.created_at DESC LIMIT ?", [*NEEDS_HUMAN, limit])
    return [_row(r) for r in rows]


def stats(conn) -> dict[str, Any]:
    end = today_local()
    days = [end - timedelta(days=i) for i in range(config.STATS_SERIES_DAYS - 1, -1, -1)]
    rows = db.query(conn, f"""SELECT {local_day()} AS day, r.decision, r.duration_ms, {TOTAL} AS total_minor,
                                     {CURRENCY} AS currency
                              FROM runs r WHERE r.status IN ('completed', 'failed') AND {local_day()} >= ?""",
                    [days[0].isoformat()])
    by_day: dict[str, list[dict[str, Any]]] = {d.isoformat(): [] for d in days}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    def series(fn) -> list[float | int | None]:
        return [fn(by_day[d.isoformat()]) for d in days]

    runs_s = series(len)
    auto_s = series(lambda rs: sum(1 for r in rs if r["decision"] == "AUTO_APPROVE"))
    rate_s = [round(a / n, 3) if n else None for a, n in zip(auto_s, runs_s)]
    held_s = series(lambda rs: sum(1 for r in rs if r["decision"] == "HOLD_FOR_REVIEW"))
    blocked_s = series(lambda rs: sum(1 for r in rs if r["decision"] == "BLOCK"))
    median_s = series(lambda rs: int(statistics.median([r["duration_ms"] for r in rs if r["duration_ms"] is not None]))
                      if any(r["duration_ms"] is not None for r in rs) else None)
    value_s = series(lambda rs: sum(r["total_minor"] or 0 for r in rs
                                    if r["decision"] == "AUTO_APPROVE" and (r["currency"] or "INR") == "INR"))

    reasons: dict[str, int] = {}
    for (raw,) in conn.execute("SELECT decision_reasons_json FROM runs WHERE decision_reasons_json IS NOT NULL"):
        for code in json.loads(raw):
            base = code.split(":")[0]
            reasons[base] = reasons.get(base, 0) + 1
    marks = ",".join("?" * len(NEEDS_HUMAN))
    queue = conn.execute(f"SELECT COUNT(*) FROM runs r WHERE r.decision IN ({marks}) AND {RESOLUTION} IS NULL",
                         NEEDS_HUMAN).fetchone()[0]

    def tile(key, label, s, fmt):
        return {"key": key, "label": label, "series": s, "value": s[-1], "display": fmt(s[-1])}

    return {
        "days": [d.isoformat() for d in days],
        "today": end.isoformat(),
        "tiles": [
            tile("runs", "Runs today", runs_s, lambda v: f"{v:,}"),
            tile("auto_rate", "Auto-approval rate", rate_s, lambda v: "—" if v is None else f"{v * 100:.0f}%"),
            tile("held", "Held for review", held_s, lambda v: f"{v:,}"),
            tile("blocked", "Blocked", blocked_s, lambda v: f"{v:,}"),
            tile("median_ms", "Median processing time", median_s,
                 lambda v: "—" if v is None else (f"{v / 1000:.1f} s" if v >= 1000 else f"{v} ms")),
            # Whole rupees on the tile; exact paise are in the runs table.
            tile("value_auto", "Value auto-approved", value_s, lambda v: format_money(v - v % 100)[:-3]),
        ],
        "reasons": sorted(({"code": k, "count": v} for k, v in reasons.items()), key=lambda x: -x["count"]),
        "queue_count": queue,
    }


def vendors(conn) -> list[dict[str, Any]]:
    return db.query(conn, "SELECT id, legal_name, status, default_currency FROM vendors ORDER BY legal_name")


def purchase_order(conn, po_number: str) -> dict[str, Any] | None:
    po = db.query_one(conn, """SELECT p.*, v.legal_name AS vendor_name FROM purchase_orders p
                               JOIN vendors v ON v.id = p.vendor_id WHERE p.po_number = ?""", (po_number,))
    if po is None:
        return None
    po["lines"] = db.query(conn, "SELECT sku, description, qty, uom, unit_price_minor, line_total_minor "
                                 "FROM po_line_items WHERE po_id = ? ORDER BY id", (po["id"],))
    grns = db.query(conn, "SELECT grn_number, received_date, lines_json FROM goods_receipts WHERE po_id = ? "
                          "ORDER BY received_date", (po["id"],))
    for g in grns:
        g["lines"] = json.loads(g.pop("lines_json"))
    po["goods_receipts"] = grns
    po["invoices"] = db.query(conn, "SELECT invoice_number, invoice_date, total_minor, status FROM invoice_history "
                                    "WHERE po_number = ? ORDER BY invoice_date", (po_number,))
    po["allow_partial_invoicing"] = bool(po["allow_partial_invoicing"])
    return po
