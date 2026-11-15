"""S08 Rules: run every rule against ctx and persist one row per rule."""
from __future__ import annotations

import json
from collections import Counter

from app import db
from app.models import RunContext, StageResult, to_jsonable
from app.rules.engine import run_all


def run(ctx: RunContext) -> StageResult:
    results = run_all(ctx)
    ctx.rule_results = results
    if ctx.persist:
        with db.session(ctx.db_path) as conn:
            conn.execute("DELETE FROM rule_results WHERE run_id = ?", (ctx.run_id,))  # a re-run replaces, never appends
            conn.executemany(
                """INSERT INTO rule_results (run_id, rule_code, rule_name, category, severity, status,
                                             message, evidence_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(ctx.run_id, r.code, r.name, r.category, r.severity, r.status, r.message,
                  json.dumps(to_jsonable({**r.evidence, "reason_codes": r.reason_codes}), ensure_ascii=False))
                 for r in results])
    counts = Counter(r.status for r in results)
    fails = [r for r in results if r.status == "fail"]
    hard = sum(1 for r in fails if r.severity == "hard")
    summary = (f"{len(results)} rules: {counts['pass']} pass, {counts['fail']} fail"
               + (f" ({hard} hard)" if fails else "")
               + (f", {counts['warn']} warn" if counts["warn"] else "")
               + f", {counts['not_applicable']} n/a"
               + (f" — {', '.join(r.code.split('_')[0] for r in fails)}" if fails else ""))
    detail = {"rules": [to_jsonable(r) for r in results], "counts": dict(counts)}
    return StageResult("warn" if fails else "ok", summary, detail)
