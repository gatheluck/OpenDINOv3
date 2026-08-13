"""The pre-registered thresholds and the code that applies them must agree.

A pre-registration is only worth something if the thresholds cannot be moved
once the result is in. Keeping the numbers in two places — a document nobody
runs and a constant nobody reads — is how they quietly diverge.

This test does not check that the thresholds are correct. It checks that the
protocol and the analysis are talking about the same ones.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opendinov3.core import download_stats as ds

DOC = Path(__file__).resolve().parent.parent / "docs" / "experiments" / (
    "0002-download-concurrency.md"
)

REGISTERED = (
    "MIN_SPEEDUP",
    "MAX_YIELD_DROP",
    "MAX_DNS_RISE",
    "MAX_BASELINE_DRIFT",
)


def read_threshold_table() -> dict[str, str]:
    """Constant name to the value written beside it in the doc's table."""
    rows = re.findall(
        r"^\|[^|]*\|\s*`([A-Z_]+)`\s*\|\s*`([^`]+)`\s*\|",
        DOC.read_text(),
        re.MULTILINE,
    )
    return dict(rows)


def test_the_protocol_document_exists() -> None:
    assert DOC.is_file(), f"missing pre-registration: {DOC}"


@pytest.mark.parametrize("name", REGISTERED)
def test_each_threshold_matches_the_document(name: str) -> None:
    table = read_threshold_table()
    assert name in table, (
        f"{name} is applied by the analysis but is not in the doc's "
        "threshold table"
    )
    assert float(table[name]) == pytest.approx(getattr(ds, name)), (
        f"{name}: doc says {table[name]}, code says {getattr(ds, name)}"
    )


def test_the_document_lists_no_threshold_the_code_does_not_apply() -> None:
    """A row nobody enforces reads as a criterion but is decoration."""
    assert set(read_threshold_table()) == set(REGISTERED)
