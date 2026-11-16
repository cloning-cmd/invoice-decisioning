"""Human actions on a decided run: override approve / reject, add a note, and
supply a missing field. Every action needs a note and writes audit_log.

The system decision on the run is never rewritten by an override: the audit
trail records the human resolution next to it, so "what the controls said" and
"what a person decided" both stay visible. Supplying a field is different: it
corrects the reading, so S05 onward re-runs with the clerk's value and the run
gets a new system decision (and its own decision_made entry).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import config, db
from app.pipeline import runner
from app.services import llm

ACTIONS = ("override_approve", "override_reject", "note_added", "field_supplied")


class ActionError(ValueError):
    """Rejected request; the message is shown to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def supplied_fields(conn, run_id: str) -> dict[str, str]:
    """Every value clerks have supplied on this run so far (later entries win)."""
    out: dict[str, str] = {}
    for r in db.query(conn, "SELECT to_value FROM audit_log WHERE run_id = ? AND action = 'field_supplied' "
                            "ORDER BY id", (run_id,)):
        v = json.loads(r["to_value"])
        out[v["field"]] = v["value"]
    return out


def suppliable_fields(raw_payload: dict[str, Any]) -> list[str]:
    """Required fields the model could not read, or read below the confidence floor.

    Only required fields: an empty optional field (no discount printed) is not
    something to type in, and a clerk re-keying arbitrary values would be data
    entry, not exception handling.
    """
    fields = dict(llm.iter_fields(raw_payload))
    return [path for path in config.REQUIRED_FIELDS
            if path in fields and (fields[path].get("value") is None
                                   or (fields[path].get("confidence") or 0) < config.CONFIDENCE_MIN)]


def perform(run_id: str, action: str, actor: str, note: str, payload: dict[str, Any] | None = None,
            db_path: Path | None = None) -> dict[str, Any]:
    note = (note or "").strip()
    if action not in ACTIONS:
        raise ActionError(f"unknown action {action!r}")
    if actor not in config.ACTION_ACTORS:
        raise ActionError(f"unknown role {actor!r}")
    if not note:
        raise ActionError("a note is required: the audit trail must say why")

    with db.session(db_path) as conn:
        run = db.query_one(conn, "SELECT id, status, decision, raw_llm_json FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise ActionError(f"no run {run_id}", 404)
        if run["status"] not in ("completed", "failed") or not run["decision"]:
            raise ActionError("this run has not reached a decision yet", 409)
        verdict = run["decision"]

        if action in ("override_approve", "override_reject"):
            allowed = config.OVERRIDE_ROLES[verdict]
            if actor not in allowed:
                who = " or ".join(config.OWNERS[r].split(", ")[1] for r in allowed)
                raise ActionError(f"a {verdict} decision can only be overridden by the {who}", 403)
            conn.execute("INSERT INTO audit_log (run_id, actor, action, from_value, to_value, note, created_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (run_id, actor, action, verdict,
                          "APPROVED" if action == "override_approve" else "REJECTED", note, _now()))
            return {"run_id": run_id, "action": action}

        if action == "note_added":
            conn.execute("INSERT INTO audit_log (run_id, actor, action, from_value, to_value, note, created_at) "
                         "VALUES (?,?,?,NULL,NULL,?,?)", (run_id, actor, action, note, _now()))
            return {"run_id": run_id, "action": action}

        # field_supplied
        field = (payload or {}).get("field")
        value = str((payload or {}).get("value") or "").strip()
        if not run["raw_llm_json"]:
            raise ActionError("this run has no stored reading to correct (its extraction never succeeded)", 409)
        raw = json.loads(run["raw_llm_json"])["payload"]
        allowed_fields = suppliable_fields(raw)
        if field not in allowed_fields:
            raise ActionError(f"{field!r} cannot be supplied; fields the model could not read confidently: "
                              f"{', '.join(allowed_fields) or 'none'}")
        if not value:
            raise ActionError("the supplied value is empty")
        old = next(f.get("value") for path, f in llm.iter_fields(raw) if path == field)
        overrides = {**supplied_fields(conn, run_id), field: value}
        conn.execute("INSERT INTO audit_log (run_id, actor, action, from_value, to_value, note, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (run_id, actor, action, json.dumps({"field": field, "value": old}),
                      json.dumps({"field": field, "value": value}), note, _now()))

    # Re-run S05 onward with the clerk's value. The model is not called again.
    ctx = runner.replay(run_id, db_path=db_path, overrides=overrides, persist=True)
    return {"run_id": run_id, "action": action, "verdict": ctx.decision.verdict if ctx.decision else None,
            "reason_codes": ctx.decision.reason_codes if ctx.decision else []}
