"""Contract for choosing between the four fetch settings.

The trap this exists to avoid: ranking the arms by speed. `retries 0` is
faster per URL almost by definition, and it can be faster while storing
FEWER images, because a URL that would have succeeded on the second attempt
is now a failure. An arm that finishes sooner with less data is not better.

So the metric is images stored per node-hour, and the yield is reported
next to it so a drop cannot hide inside a speedup.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts"
          / "compare_arms.py")


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def arm(root: Path, task: int, *, threads: int, retries: int,
        wall: int, candidates: int, successes: int) -> None:
    task_dir = root / f"task-{task:06d}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "DONE.json").write_text(json.dumps({
        "task_id": task, "wall_seconds": wall,
        "candidates": candidates, "successes": successes,
        "yield": successes / candidates,
        "settings": {"threads": threads, "retries": retries,
                     "processes": 32, "timeout": 10},
    }))


def control(root: Path, **kw) -> None:
    defaults = dict(threads=32, retries=2, wall=3600,
                    candidates=100_000, successes=58_700)
    arm(root, 8, **{**defaults, **kw})


def test_the_arms_are_ranked_by_images_stored_per_node_hour(tmp_path
                                                            ) -> None:
    control(tmp_path)
    arm(tmp_path, 9, threads=128, retries=2, wall=1200,
        candidates=100_000, successes=58_000)
    out = tmp_path / "c.json"
    result = run(tmp_path, "--json", out)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(out.read_text())
    assert payload["best"]["threads"] == 128
    assert payload["best"]["speedup"] == pytest.approx(58_000 / 58_700 * 3, 0.01)


def test_an_arm_that_is_faster_but_stores_less_is_flagged(tmp_path) -> None:
    """The whole point. retries=0 turns a URL that would have succeeded on
    the second attempt into a failure, so it can win on wall time while
    losing images — and the corpus is the images."""
    control(tmp_path)
    arm(tmp_path, 10, threads=32, retries=0, wall=1200,
        candidates=100_000, successes=30_000)   # 3x faster, half the yield
    result = run(tmp_path, "--json", tmp_path / "c.json")
    assert "yield" in result.stdout.lower()
    assert "30.0%" in result.stdout


def test_a_yield_collapse_in_the_winner_is_called_out(tmp_path) -> None:
    control(tmp_path)
    arm(tmp_path, 10, threads=32, retries=0, wall=600,
        candidates=100_000, successes=20_000)
    result = run(tmp_path, "--json", tmp_path / "c.json")
    combined = result.stdout + result.stderr
    assert "loses" in combined.lower() or "lower yield" in combined.lower()


def test_arms_that_all_perform_the_same_mean_no_setting_helps(tmp_path
                                                              ) -> None:
    """Then the bottleneck is shared — DNS is the suspect — and adding
    threads would be a wave spent on nothing."""
    for index, (task, threads, retries) in enumerate(
            [(8, 32, 2), (9, 128, 2), (10, 32, 0), (11, 128, 0)]):
        arm(tmp_path, task, threads=threads, retries=retries, wall=3600,
            candidates=100_000, successes=58_700)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no setting" in result.stdout.lower()


def test_an_arm_that_did_not_finish_is_named_not_averaged_away(tmp_path
                                                               ) -> None:
    control(tmp_path)
    (tmp_path / "task-000009").mkdir(parents=True)     # started, never done
    result = run(tmp_path, "--json", tmp_path / "c.json")
    assert "task-000009" in result.stdout
    assert json.loads((tmp_path / "c.json").read_text())["arms_missing"] == 1


def test_a_single_arm_cannot_be_compared(tmp_path) -> None:
    """One result is a measurement, not a comparison, and reporting a
    winner from it would read as though something had been chosen."""
    control(tmp_path)
    assert run(tmp_path).returncode != 0


def test_the_full_corpus_projection_uses_the_winner(tmp_path) -> None:
    control(tmp_path)
    arm(tmp_path, 9, threads=128, retries=2, wall=900,
        candidates=100_000, successes=58_000)
    out = tmp_path / "c.json"
    run(tmp_path, "--json", out)
    payload = json.loads(out.read_text())
    # 1,387,173,656 URLs at 100,000 per 900 s
    assert payload["projected_node_hours"] == pytest.approx(
        1_387_173_656 / (100_000 / 900) / 3600, rel=0.01)


def test_nothing_to_compare_is_an_error(tmp_path) -> None:
    assert run(tmp_path).returncode != 0


def test_speed_and_stored_images_can_rank_the_arms_differently(tmp_path
                                                               ) -> None:
    """The thesis of this whole script, as a fixture.

    An arm can process URLs three times faster and still store fewer
    images: retries=0 turns second-attempt successes into failures. Ranking
    by URLs/s would pick it, and the corpus would come out smaller from the
    same node-hours.

    control : 100,000 URLs / 3600 s -> 27.8 URLs/s, 16.3 stored/s
    fast    : 100,000 URLs / 1200 s -> 83.3 URLs/s, 12.5 stored/s
    """
    control(tmp_path)                                    # 58,700 stored
    arm(tmp_path, 10, threads=32, retries=0, wall=1200,
        candidates=100_000, successes=15_000)
    out = tmp_path / "c.json"
    assert run(tmp_path, "--json", out).returncode == 0
    payload = json.loads(out.read_text())
    assert payload["arms"][0]["task"] == 8, (
        "ranked the arm that processed more URLs while storing fewer images")
    # And the projection must follow the arm that was actually chosen.
    assert payload["best"]["task"] == 8
