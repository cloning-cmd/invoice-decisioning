"""S03 OCR — the scanner path.

Phase 1 placeholder that is honest about what it does: it runs only when the
document is scanned, and until Phase 3 lands it reports `warn` and sends S04
down the vision-only path rather than pretending OCR happened.
"""
from __future__ import annotations

from app.models import RunContext, StageResult


def run(ctx: RunContext) -> StageResult:
    if not ctx.is_scanned:
        return StageResult("skipped", "digital text layer present — OCR not needed", {"reason": "not scanned"})
    ctx.ocr_unavailable = True
    return StageResult("warn", "scanned input; OCR is not built yet (Phase 3) — extraction proceeds vision-only",
                       {"ocr_unavailable": True, "reason": "scanner path not implemented in this build"})
