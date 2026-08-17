"""The planning constants must be measurements, with their provenance.

Sizing a walltime from a derived figure presented as measured cost this
project a whole wave: 8 subjobs killed at 2 hours because 1.01 h/task came
from 650000/SUCC_PER_SEC/3600 and was written up as "a task measured".

Every constant here now has a measurement behind it. These tests pin the
values against what was observed, so a silent edit fails rather than
quietly changing 60 TB back into 23 TB.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import plan_partition as pp  # noqa: E402

SOURCE = (Path(__file__).resolve().parent.parent / "scripts"
          / "plan_partition.py").read_text()


def test_the_throughput_is_the_measured_one() -> None:
    """Experiment 0004 arm 10 (32 threads, retries 0) on a full 100,000-URL
    slice: 348.4 URLs/s at 64.7% yield, so 225.3 stored/s."""
    assert pp.SUCC_PER_SEC == 225.3


def test_the_bytes_per_image_is_the_measured_one() -> None:
    """od.sh report on our own output: 692,851 images, 85.6 KB each. The
    old 25.1 came from the predecessor's tree, whose pipeline settings were
    never verified — and it under-predicted storage by 2.6x."""
    assert pp.KB_PER_IMAGE == 85.6


def test_the_yield_is_within_what_was_observed() -> None:
    """55.0% over four tasks, 61.9% and 62.2% on two more, 64.7% on the
    experiment arm. The constant must sit inside that range, not above it."""
    assert 0.55 <= pp.YIELD <= 0.65


def test_every_constant_names_where_it_came_from() -> None:
    """A number without provenance is how 1.01 h/task became 'measured'."""
    for name in ("SUCC_PER_SEC", "KB_PER_IMAGE", "YIELD"):
        block = SOURCE.split(f"{name} =")[0].rsplit("\n\n", 1)[-1]
        assert re.search(r"20\d\d-\d\d-\d\d", block), (
            f"{name} has no dated measurement in its comment")


def test_the_warning_about_a_stale_rate_is_gone_once_it_is_measured() -> None:
    """The flag existed to say the figure was historical. It is not any
    more, and a warning that no longer applies trains people to ignore
    warnings."""
    assert pp.SUCC_PER_SEC_IS_MEASURED_ON_A_CURRENT_WAVE is True
