"""Rule protocol, registry, and runner.

A rule is a pure function (ctx) -> RuleResult: it reads ctx, never mutates it,
never touches the network or the database, and never raises. Every rule runs
on every invoice — no short-circuiting — so the UI shows the full diagnosis.
"""
from __future__ import annotations

import dataclasses
import traceback
from dataclasses import dataclass
from typing import Callable

from app.models import RuleResult, RunContext, Severity

RuleFn = Callable[[RunContext], RuleResult]


@dataclass(frozen=True)
class RuleSpec:
    code: str
    name: str
    category: str
    severity: Severity
    fn: RuleFn


REGISTRY: list[RuleSpec] = []


def rule(code: str, name: str, category: str, severity: Severity) -> Callable[[RuleFn], RuleFn]:
    """Register a rule. Registration order is display order."""
    def decorate(fn: RuleFn) -> RuleFn:
        REGISTRY.append(RuleSpec(code, name, category, severity, fn))
        return fn
    return decorate


# Constructors rules use to build their result; the engine stamps the metadata.
def passed(message: str, **evidence) -> RuleResult:
    return RuleResult("pass", message, evidence)


def failed(message: str, reasons: list[str], **evidence) -> RuleResult:
    return RuleResult("fail", message, evidence, reasons)


def warned(message: str, **evidence) -> RuleResult:
    return RuleResult("warn", message, evidence)


def not_applicable(message: str, **evidence) -> RuleResult:
    return RuleResult("not_applicable", message, evidence)


def run_one(spec: RuleSpec, ctx: RunContext) -> RuleResult:
    try:
        result = spec.fn(ctx)
    except Exception as exc:  # noqa: BLE001 — a crashing rule must fail closed, visibly
        # A rule bug is reported as a failure of that rule (with its severity), never as a pass.
        result = RuleResult("fail", f"rule crashed: {type(exc).__name__}: {exc}",
                            {"traceback": traceback.format_exc(limit=4)}, ["RULE_ERROR"])
    return dataclasses.replace(result, code=spec.code, name=spec.name,
                               category=spec.category, severity=spec.severity)


def run_all(ctx: RunContext) -> list[RuleResult]:
    from app.rules import definitions  # noqa: F401 — importing registers the rules

    # Sorted by code so the table always reads R01, R02, R03, R03b, … regardless of file layout.
    return [run_one(spec, ctx) for spec in sorted(REGISTRY, key=lambda s: s.code)]
