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


# --------------------------------------------------------------------------
# What img2dataset is told, against what DataComp itself passes
# --------------------------------------------------------------------------

RUNNER = (Path(__file__).resolve().parent.parent / "scripts"
          / "production_task.sh")


def test_the_caption_column_is_passed_to_img2dataset() -> None:
    """DataComp's own download_upstream.py passes caption_col="text".

    Without it the shards hold no captions, and the text-to-image stage that
    video models train first becomes impossible from this corpus.
    """
    assert "--caption_col text" in RUNNER.read_text()


def test_the_upstream_identifier_is_preserved() -> None:
    """DataComp passes save_additional_columns=["uid"]. It is how a sample
    is traced back to upstream metadata."""
    assert "uid" in RUNNER.read_text()
    assert "--save_additional_columns" in RUNNER.read_text()


def test_face_blurring_must_be_chosen_explicitly() -> None:
    """Irreversible, applies to 902 million images, and legally relevant.

    DataComp blurs by default; a silent default either way decides a
    compliance question by accident, so the run refuses until it is set.
    """
    runner = RUNNER.read_text()
    assert "OD_BLUR_FACES is not set" in runner
    assert "exit 2" in runner
    # Behaviour, not spelling: the refusal itself is exercised in
    # tests/test_production_task.py.


def test_the_document_records_the_choice_and_that_it_is_irreversible() -> None:
    doc = DOC.read_text()
    assert "face" in doc.lower()
    assert "irreversible" in doc.lower()
    assert "OD_BLUR_FACES" in doc
