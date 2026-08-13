"""The 0003 analysis must read back what the job writes — including a
multi-node phase that never ran.

Launching across nodes is the fragile part of that job, and the single-node
phases run first precisely so a launch failure still leaves something usable.
That path therefore has to be exercised: reporting "NOT RUN" as though it
were a result would be worse than the failure itself.

Builds output trees in the layout the job produces and runs the real script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / (
    "analyse_experiment_0003.py"
)
DNS_KEY = "<urlopen error [Errno -2] Name or service not known>"


def write_phase(root: Path, phase: str, nodes: int, wall: float,
                successes_per_node: int, dns_share: float = 0.081,
                yield_rate: float = 0.865) -> None:
    """One phase's output tree.

    Failures scale with the node's share of the work. Every phase downloads
    the same number of URLs in the real experiment, so holding the failure
    *counts* fixed while splitting the successes would make the multi-node
    phase look worse purely as an artifact of the fixture.
    """
    phase_dir = root / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "wall_seconds").write_text(f"{wall}\n")
    for node in range(nodes):
        shards = phase_dir / f"node{node}" / "run1_p16" / "shards"
        shards.mkdir(parents=True)
        candidates = round(successes_per_node / yield_rate)
        dns = round(candidates * dns_share)
        status = {
            "success": successes_per_node,
            DNS_KEY: dns,
            "HTTP Error 404: Not Found": candidates - successes_per_node - dns,
        }
        (shards / "00000_stats.json").write_text(json.dumps({
            "count": sum(status.values()),
            "successes": successes_per_node,
            "status_dict": status,
        }))


def run(outdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), str(outdir)],
                          capture_output=True, text=True)


@pytest.fixture
def neutral_tree(tmp_path) -> Path:
    write_phase(tmp_path, "phase1_single", 1, 100.0, 6400)
    write_phase(tmp_path, "phase2_multi", 2, 100.0, 3200)
    write_phase(tmp_path, "phase3_single", 1, 100.0, 6400)
    return tmp_path


def test_a_neutral_result_is_not_rejected(neutral_tree: Path) -> None:
    """Two nodes at half the processes each deliver the same total."""
    result = run(neutral_tree)
    assert result.returncode == 0, result.stderr
    assert "NOT REJECTED" in result.stdout
    assert "distribution free: True" in result.stdout


def test_the_multi_node_phase_is_summed_across_its_nodes(
    neutral_tree: Path,
) -> None:
    """Each node did 3,200; the phase did 6,400. Reading one node would
    halve the throughput and invent a distribution penalty."""
    result = run(neutral_tree)
    line = next(l for l in result.stdout.splitlines()
                if l.startswith("phase2_multi"))
    assert " 2 " in line, f"node count not 2: {line}"
    assert "6400" in line, f"successes not summed: {line}"


def test_a_throughput_collapse_is_rejected(tmp_path) -> None:
    write_phase(tmp_path, "phase1_single", 1, 100.0, 6400)
    write_phase(tmp_path, "phase2_multi", 2, 400.0, 3200)   # 4× slower
    write_phase(tmp_path, "phase3_single", 1, 100.0, 6400)
    result = run(tmp_path)
    # "REJECTED" alone would also match "NOT REJECTED".
    assert "NOT REJECTED" not in result.stdout
    assert "distribution free: False" in result.stdout


def test_a_missing_multi_node_phase_is_reported_as_unanswered(
    tmp_path,
) -> None:
    """The expected failure mode, and the one worth getting right."""
    write_phase(tmp_path, "phase1_single", 1, 100.0, 6400)
    write_phase(tmp_path, "phase3_single", 1, 100.0, 6300)
    (tmp_path / "phase2_multi_SKIPPED").write_text("skipped: no launcher\n")

    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "NOT RUN" in result.stdout
    assert "skipped: no launcher" in result.stdout
    assert "unanswered" in result.stdout
    # An absent phase is not a falsification.
    assert "NOT REJECTED" in result.stdout


def test_drift_between_the_single_node_phases_is_reported(tmp_path) -> None:
    write_phase(tmp_path, "phase1_single", 1, 100.0, 6400)
    write_phase(tmp_path, "phase2_multi", 2, 100.0, 3200)
    write_phase(tmp_path, "phase3_single", 1, 200.0, 6400)  # half the rate
    result = run(tmp_path)
    assert "baseline drift" in result.stdout
    assert "stable: False" in result.stdout
    assert "NOT REJECTED" not in result.stdout


def test_an_empty_directory_fails_rather_than_reporting_nothing(
    tmp_path,
) -> None:
    result = run(tmp_path)
    assert result.returncode == 2
    assert "no single-node phase" in result.stderr
