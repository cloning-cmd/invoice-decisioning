"""Console runner.

  python -m app.cli run <file>              process one invoice, print the trace and decision
  python -m app.cli run --replay <run_id>   re-decide a stored run with the current config.py
                                            (no model call, nothing written)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

from app import config, db
from app.models import RunContext
from app.pipeline import runner

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
_CODES = {"green": "32", "amber": "33", "red": "31", "blue": "34", "grey": "90", "bold": "1"}
STAGE_GLYPH = {"ok": ("✓", "green"), "warn": ("!", "amber"), "failed": ("✗", "red"),
               "skipped": ("–", "grey"), "running": ("…", "blue")}
RULE_GLYPH = {"pass": ("PASS", "green"), "warn": ("WARN", "amber"), "fail": ("FAIL", "red"),
              "not_applicable": ("n/a ", "grey")}
VERDICT_COLOR = {"AUTO_APPROVE": "green", "APPROVE_PENDING_SIGNOFF": "green", "HOLD_FOR_REVIEW": "amber",
                 "REJECT": "red", "BLOCK": "red"}


def c(text: str, color: str) -> str:
    return f"\033[{_CODES[color]}m{text}\033[0m" if _COLOR else text


def wrap(text: str, indent: int) -> str:
    return textwrap.fill(text, width=110, initial_indent=" " * indent, subsequent_indent=" " * indent)


def print_stage(event: str, row: dict[str, Any]) -> None:
    if event != "stage_finished":
        return
    glyph, color = STAGE_GLYPH[row["status"]]
    ms = f"{row['duration_ms']:>6} ms" if row.get("duration_ms") is not None else " " * 9
    print(f"  {c(glyph, color)} {row['seq']:02d} {row['stage_label']:<11} {ms}  {row['summary']}")
    if row["status"] == "failed" and row.get("detail", {}).get("traceback"):
        print(c(textwrap.indent(row["detail"]["traceback"].rstrip(), " " * 8), "grey"))


def print_outcome(ctx: RunContext) -> None:
    if ctx.rule_results:
        print(f"\n  {c('Rules', 'bold')}")
        for r in ctx.rule_results:
            label, color = RULE_GLYPH[r.status]
            sev = r.severity[0].upper()
            print(f"  {c(label, color)} {sev} {r.code:<28} " + textwrap.shorten(r.message, 150))
    d = ctx.decision
    print()
    if d is None:
        print(c("  No decision was produced.", "red"))
        return
    print(f"  {c('Decision', 'bold')}  {c(d.verdict, VERDICT_COLOR[d.verdict])}"
          + (f"   reasons: {', '.join(d.reason_codes)}" if d.reason_codes else ""))
    print(wrap(d.explanation, 12))
    print(f"  {'Next':<9} {d.next_action}")
    print(f"  {'Owner':<9} {d.owner}")
    print(f"  {'Branch':<9} {d.branch}")
    if d.payment_date:
        print(f"  {'Pay on':<9} {d.payment_date.isoformat()}")
    from app.rules.definitions import run_confidence
    if ctx.invoice:
        print(f"  {'Conf.':<9} {run_confidence(ctx)}")
    print(f"  {'Run':<9} {ctx.run_id}")


def cmd_run(args: argparse.Namespace) -> int:
    db.init_db()
    if args.replay:
        with db.session() as conn:
            stored = db.query_one(conn, "SELECT decision, decision_reasons_json, source_filename FROM runs "
                                        "WHERE id = ?", (args.replay,))
        if stored is None:
            print(f"error: no run {args.replay}", file=sys.stderr)
            return 2
        print(f"\n  Replaying run {args.replay} · {stored['source_filename']} "
              f"(stored extraction, current config, nothing written)\n")
        ctx = runner.replay(args.replay, emit=print_stage)
        print_outcome(ctx)
        new = ctx.decision.verdict if ctx.decision else None
        old_reasons = json.loads(stored["decision_reasons_json"] or "[]")
        changed = new != stored["decision"] or (ctx.decision and ctx.decision.reason_codes != old_reasons)
        print(f"\n  Stored decision {stored['decision']} {old_reasons} → replayed {new} "
              f"{ctx.decision.reason_codes if ctx.decision else ''}  "
              + (c("CHANGED", "amber") if changed else c("unchanged", "grey")))
        return 0
    if not args.file:
        print("error: give a file, or --replay <run_id>", file=sys.stderr)
        return 2
    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file {path}", file=sys.stderr)
        return 2
    print(f"\n  {c('Invoice run', 'bold')} · {path.name} · LLM {config.LLM_CACHE_MODE} ({config.LLM_MODEL})\n")
    ctx = runner.run_file(path.read_bytes(), path.name, emit=print_stage)
    print_outcome(ctx)
    print()
    return 1 if ctx.failed_stage else 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, which cannot print ₹.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="process an invoice file, or replay a stored run")
    p_run.add_argument("file", nargs="?")
    p_run.add_argument("--replay", metavar="RUN_ID", help="re-decide a stored run with the current config")
    p_run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
