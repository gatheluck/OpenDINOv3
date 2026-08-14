"""The production thresholds and the code applying them must agree.

Same reasoning as the experiment documents: a threshold recorded in prose
nobody runs and a constant nobody reads will diverge, and the first anyone
notices is when a run behaves differently from how it is documented.

This checks agreement, not correctness.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opendinov3.core import task_health as th

DOC = Path(__file__).resolve().parent.parent / "docs" / "production.md"

REGISTERED = (
    "MAX_UNREACHABLE_FRACTION",
    "MAX_DNS_FRACTION",
    "MIN_YIELD",
)


def threshold_table() -> dict[str, str]:
    return dict(re.findall(
        r"^\|[^|]*\|\s*`([A-Z_]+)`\s*\|\s*`([^`]+)`\s*\|",
        DOC.read_text(), re.MULTILINE))


def test_the_production_document_exists() -> None:
    assert DOC.is_file(), f"missing: {DOC}"


@pytest.mark.parametrize("name", REGISTERED)
def test_each_threshold_matches_the_document(name: str) -> None:
    table = threshold_table()
    assert name in table, f"{name} is applied but is not in the doc's table"
    assert float(table[name]) == pytest.approx(getattr(th, name)), (
        f"{name}: doc says {table[name]}, code says {getattr(th, name)}")


def test_the_document_lists_no_threshold_the_code_does_not_apply() -> None:
    assert set(threshold_table()) == set(REGISTERED)


def test_the_documented_settings_match_the_scripts() -> None:
    """The doc justifies 32 processes and 10,000 samples per shard from
    measurements. If a script drifts from that, the justification is stale.
    """
    doc = DOC.read_text()
    runner = (Path(__file__).resolve().parent.parent / "scripts"
              / "production_task.sh").read_text()
    submit = (Path(__file__).resolve().parent.parent / "scripts"
              / "submit_production.sh").read_text()

    assert "OD_PROCESSES:-32}" in runner
    assert "OD_SAMPLES_PER_SHARD:-10000}" in runner
    assert "OD_PROCESSES:-32}" in submit
    assert "OD_SAMPLES_PER_SHARD:-10000}" in submit
    assert "32 processes per node" in doc
    assert "10,000 samples per shard" in doc


def test_the_documented_scale_matches_the_measured_metadata() -> None:
    """1,388 tasks is a measurement, not a round number: 1,387,173,656 rows
    at 1,000,000 per task."""
    doc = DOC.read_text()
    assert "1,387,173,656" in doc
    assert "1,388" in doc
    assert "173,656" in doc, "the short final task must be stated"
