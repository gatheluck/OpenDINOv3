"""Contract for the single rule that decides whether a task is finished.

It had two implementations. The runner compared recorded candidates against
the plan; the wave summary looked for a `partial` flag the runner no longer
used. A wave reported "3 complete, will be skipped" for three tasks it was
about to redo. Harmless that time, and the two were already disagreeing
about the state of the corpus.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import is_task_complete as itc  # noqa: E402


def write(tmp_path: Path, *, candidates=None, rows=1_000_000, task_id=8,
          marker_extra=None):
    marker = tmp_path / "DONE.json"
    body = {"task_id": task_id}
    if candidates is not None:
        body["candidates"] = candidates
    body.update(marker_extra or {})
    marker.write_text(json.dumps(body))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(
        {"tasks": [{"task_id": task_id, "rows": rows, "pieces": []}]}))
    return str(marker), str(plan)


def test_a_marker_covering_the_plan_is_skipped(tmp_path) -> None:
    marker, plan = write(tmp_path, candidates=1_000_000)
    assert itc.verdict(marker, plan, 8) == "skip"


def test_a_marker_short_of_the_plan_is_redone(tmp_path) -> None:
    """Tasks 8-10 on the cluster: 100,000 against a plan of 1,000,000."""
    marker, plan = write(tmp_path, candidates=100_000)
    assert itc.verdict(marker, plan, 8).startswith("redo")


def test_the_flag_is_not_what_decides(tmp_path) -> None:
    """The markers that caused this carry no flag at all, so a rule keyed on
    one skips them forever."""
    marker, plan = write(tmp_path, candidates=100_000)
    assert itc.verdict(marker, plan, 8).startswith("redo")
    marker2, plan2 = write(tmp_path, candidates=1_000_000,
                           marker_extra={"partial": True})
    # Even claiming to be partial, it covers the plan; the count decides.
    assert itc.verdict(marker2, plan2, 8) == "skip"


def test_a_marker_with_no_candidates_is_redone(tmp_path) -> None:
    marker, plan = write(tmp_path)
    assert itc.verdict(marker, plan, 8) == "redo unverifiable"


def test_a_task_missing_from_the_plan_is_redone(tmp_path) -> None:
    marker, plan = write(tmp_path, candidates=1_000_000, task_id=8)
    assert itc.verdict(marker, plan, 99) == "redo unverifiable"


def test_an_unreadable_plan_is_redone(tmp_path) -> None:
    marker, _ = write(tmp_path, candidates=1_000_000)
    assert itc.verdict(marker, str(tmp_path / "nope.json"), 8) \
        == "redo unverifiable"


def test_a_missing_marker_is_redone(tmp_path) -> None:
    _, plan = write(tmp_path, candidates=1_000_000)
    assert itc.verdict(str(tmp_path / "nope.json"), plan, 8) \
        == "redo unverifiable"
