"""Contract for the four fetch-setting arms.

The experiment exists because the first wave came in at 34.9 URLs/sec/node
against a model of 277, while using 0.40% of the bandwidth and 0.53 of 192
cores. That leaves per-request latency, and the arms separate "not enough
in flight" from "something shared is saturated".

The only way this experiment can lie is by mislabelling an arm — running
one setting and recording another, or two arms landing on the same task.
Both are pinned here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JOB = REPO / "scripts" / "experiment_0004_job.sh"

ARMS = {1: ("32", "2"), 2: ("128", "2"), 3: ("32", "0"), 4: ("128", "0")}


def run(index, **extra):
    """Runs with a stub in place of production_job.sh, which is what makes
    the arm's resolved environment observable."""
    stub = REPO / "tests" / "stubs" / "echo_env_job.sh"
    return subprocess.run(
        ["bash", str(JOB)], capture_output=True, text=True,
        env={**os.environ, "PBS_ARRAY_INDEX": str(index),
             "OD_REPO": str(REPO), "OD_PRODUCTION_JOB": str(stub), **extra})


@pytest.mark.parametrize("index,expected", sorted(ARMS.items()))
def test_each_arm_sets_the_settings_it_claims(index, expected) -> None:
    threads, retries = expected
    result = run(index)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"OD_THREADS={threads}" in result.stdout, result.stdout
    assert f"OD_RETRIES={retries}" in result.stdout, result.stdout


def test_the_arms_are_all_different() -> None:
    """Two arms with the same settings would compare a thing against
    itself and read as 'concurrency does not help'."""
    assert len(set(ARMS.values())) == len(ARMS)


@pytest.mark.parametrize("index", sorted(ARMS))
def test_each_arm_lands_on_its_own_task(index) -> None:
    """Overlapping tasks would have one arm skip work the other had already
    done, and look faster for it."""
    result = run(index)
    assert f"task      : {index + 7}" in result.stdout, result.stdout


def test_an_undefined_arm_is_refused() -> None:
    """-J 1-8 instead of 1-4 must not silently run the control four extra
    times.

    Checks where it stops, not merely that it did: `set -u` makes an
    unbound OD_THREADS exit non-zero further down, which would satisfy a
    bare returncode check with the guard removed.
    """
    result = run(5)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "is not defined" in combined
    assert "unbound variable" not in combined, combined
    assert "experiment 0004" not in combined, "fell through the guard"


def test_the_slice_is_the_same_for_every_arm() -> None:
    """Different slice sizes would make the comparison meaningless."""
    sizes = {run(i).stdout.split("urls      : ")[1].split("\n")[0]
             for i in ARMS}
    assert len(sizes) == 1, sizes


def test_the_slice_fits_a_walltime_at_the_measured_rate() -> None:
    """At 34.9 URLs/sec the control must finish well inside 2 hours, or it
    measures how long a kill takes."""
    urls = int(run(1).stdout.split("urls      : ")[1].split("\n")[0])
    assert urls / 34.9 / 3600 < 1.0, f"{urls} URLs is too slow an arm"
