"""The demo sample registry: one entry per generated fixture in data/samples/.

The UI's one-click chips and POST /api/runs/from-sample read this list. The
pipeline never sees the key or the label: a sample is uploaded exactly as a
user's file would be, so no code path can tell a fixture from a real invoice.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app import config


@dataclass(frozen=True)
class Sample:
    key: str
    filename: str
    label: str
    group: str          # "scenario" (section 8 edge cases) | "format" (input-format checks)


SAMPLES: tuple[Sample, ...] = (
    Sample("HP", "HP_nordic_clean.pdf", "Happy path", "scenario"),
    Sample("EC1", "EC1_meridian_photo.jpg", "Edge 1 · Phone photo", "scenario"),
    Sample("EC2", "EC2_nordic_third_tranche.pdf", "Edge 2 · Split invoice", "scenario"),
    Sample("EC3", "EC3_shreeji_reissue.pdf", "Edge 3 · Near-duplicate", "scenario"),
    Sample("EC4", "EC4_kaveri_redirect.pdf", "Edge 4 · Bank change", "scenario"),
    Sample("EC5", "EC5_orbit_variance.pdf", "Edge 5 · Variance + short GRN", "scenario"),
    Sample("EC6", "EC6_apex_eur.pdf", "Edge 6 · EUR on a closed PO", "scenario"),
)

BY_KEY = {s.key: s for s in SAMPLES}


def path_of(sample: Sample, samples_dir: Path | None = None) -> Path:
    return Path(samples_dir or config.SAMPLES_DIR) / sample.filename
