"""The record of what the scaling measurements found, kept honest.

Numbers in prose drift away from the code that produced them. `measurements.md`
carried 25.1 KB per image long after the measured figure was 85.6, and a plan
built on the stale number under-predicted storage by 2.6x. So the figures a
decision rests on are asserted against the constants, not just written down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opendinov3.net import dns_cache
from opendinov3.core import task_health

DOCS = Path(__file__).resolve().parent.parent / "docs"
EXPERIMENT = DOCS / "experiments" / "0005-node-scaling-and-its-two-ceilings.md"
PRODUCTION = DOCS / "production.md"
RUNNER = (Path(__file__).resolve().parent.parent / "scripts"
          / "production_task.sh")


def test_the_record_exists() -> None:
    assert EXPERIMENT.is_file(), (
        "the scaling measurements decided the schedule; without the document "
        "they live only in a conversation")


@pytest.mark.parametrize("signature", ["unreachable", "76.6%", "35.3%"])
def test_both_ceilings_are_described_by_what_distinguishes_them(signature
                                                                 ) -> None:
    """Yield alone cannot tell them apart — both look like "worse". The
    failure mix is what says which one was hit, and pointing the wrong lever
    at the wrong ceiling cost two weeks."""
    assert signature in EXPERIMENT.read_text()


@pytest.mark.parametrize("switch", ["OD_DNS_CACHE", "OD_HTTP_POOL"])
def test_a_switch_the_runner_reads_is_documented(switch) -> None:
    """An operator reads production.md, not the shell script."""
    assert switch in RUNNER.read_text(), f"{switch} is not read by the runner"
    assert switch in PRODUCTION.read_text(), f"{switch} is undocumented"


def test_the_ttl_rationale_in_the_document_matches_the_constant() -> None:
    """The default is chosen to span one shard, and the shard figure is what
    makes it defensible. If one moves without the other the number becomes
    folklore."""
    text = PRODUCTION.read_text()
    assert "556" in text, "the shard duration the TTL is chosen from is missing"
    assert dns_cache.DEFAULT_TTL >= 556


def test_the_shard_inheritance_rule_is_stated_where_it_is_relied_on() -> None:
    """71 tasks were made permanently unfinishable by the gap between these
    two numbers. It should not be possible to read one without the other."""
    import sys
    sys.path.insert(0, str(RUNNER.parent))
    import prepare_retry  # noqa: PLC0415 — scripts/ is not a package

    assert prepare_retry.MIN_SHARD_YIELD == task_health.MIN_YIELD
    assert "17.4%" in EXPERIMENT.read_text(), (
        "the yield that exposed the gap is not recorded")


def test_the_spawn_defect_is_recorded() -> None:
    """The next person to add a patch will reach for the wrapper, because
    that is the obvious place, and it does not work."""
    text = EXPERIMENT.read_text() + PRODUCTION.read_text()
    assert "spawn" in text
    assert "sitecustomize" in text
