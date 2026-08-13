"""Experiment 0003's registered thresholds must match the code that applies them.

Same reasoning as the 0002 equivalent: a pre-registration whose numbers can be
edited after the result is known is not a pre-registration. Keeping them in a
document nobody runs and a constant nobody reads is how they diverge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opendinov3.core import node_plan as np

DOC = Path(__file__).resolve().parent.parent / "docs" / "experiments" / (
    "0003-node-distribution.md"
)

REGISTERED = (
    "MIN_DISTRIBUTION_RATIO",
    "MAX_YIELD_DROP",
    "MAX_DNS_RISE",
    "MAX_BASELINE_DRIFT",
)


def read_threshold_table() -> dict[str, str]:
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
        f"{name} is applied by the analysis but is not in the doc's table"
    )
    assert float(table[name]) == pytest.approx(getattr(np, name)), (
        f"{name}: doc says {table[name]}, code says {getattr(np, name)}"
    )


def test_the_document_lists_no_threshold_the_code_does_not_apply() -> None:
    assert set(read_threshold_table()) == set(REGISTERED)


def test_the_shard_granularity_table_matches_the_plan() -> None:
    """The doc states 6.25 shards per process for both phases. If the planner
    ever stops producing that, the document is claiming a property the
    experiment no longer has.
    """
    configs = np.plan_distribution(32, 200_000, 1_000, node_counts=[1, 2])
    assert all(c.waves == pytest.approx(6.25) for c in configs)
    assert "6.25" in DOC.read_text()
