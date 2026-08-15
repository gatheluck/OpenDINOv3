"""Contract for marking an already-downloaded task complete.

A task rejected under a threshold that has since been corrected still has
all its data on disk. Re-downloading a million URLs to recover a marker
file would cost a node-hour and replace good images with whatever the web
returns today.

This is deliberately narrow: it re-runs the SAME health check the task
already ran, and writes DONE.json only if that check now passes. It cannot
be used to force a task through — that is the difference between salvaging
and overriding, and the guard exists because 474 tasks were once lost to
output that looked finished.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts"
          / "salvage_task.py")

DNS = "<urlopen error [Errno -2] Name or service not known>"
UNREACHABLE = "<urlopen error [Errno 101] Network is unreachable>"


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def task(root: Path, task_id: int = 16, *, shards: int = 10,
         per_shard: int = 100_000, successes: int = 61_890,
         unreachable: int = 2_047, dns: int = 6_000) -> Path:
    """A finished task, as the cluster left it: shards and statistics, no
    DONE.json because the health check rejected it."""
    task_dir = root / f"task-{task_id:06d}"
    shard_dir = task_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for index in range(shards):
        (shard_dir / f"{index:05d}_stats.json").write_text(json.dumps({
            "count": per_shard // shards,
            "successes": successes // shards,
            "status_dict": {
                "success": successes // shards,
                UNREACHABLE: unreachable // shards,
                DNS: dns // shards,
                "HTTP Error 404: Not Found":
                    (per_shard - successes - unreachable - dns) // shards,
            },
        }))
    return task_dir


def test_a_task_healthy_under_the_corrected_check_is_marked_done(tmp_path
                                                                 ) -> None:
    """The cluster case: 61.9% yield, 2.05% unreachable. Rejected under the
    old 1% limit, healthy under the corrected one, and every image already
    on disk."""
    task_dir = task(tmp_path)
    assert not (task_dir / "DONE.json").exists()
    result = run(task_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    done = json.loads((task_dir / "DONE.json").read_text())
    assert done["task_id"] == 16
    # Divisible by the shard count, so the fixture is exact.
    assert done["successes"] == 61_890
    assert done["salvaged"] is True


def test_a_task_that_is_still_unhealthy_is_refused(tmp_path) -> None:
    """Salvaging is not overriding. A task that fails the check now must
    stay unmarked, or the guard that saved 474 tasks becomes optional."""
    task_dir = task(tmp_path, successes=1_000, unreachable=155_000,
                    dns=706_000, per_shard=1_000_000)
    result = run(task_dir)
    assert result.returncode != 0
    assert not (task_dir / "DONE.json").exists()


def test_an_already_complete_task_is_left_alone(tmp_path) -> None:
    """Rewriting DONE.json would replace a real wall_seconds with an
    estimate and make the throughput record worse."""
    task_dir = task(tmp_path)
    original = json.dumps({"task_id": 16, "successes": 999, "wall_seconds": 42})
    (task_dir / "DONE.json").write_text(original)
    result = run(task_dir)
    assert result.returncode == 0
    assert (task_dir / "DONE.json").read_text() == original


def test_the_marker_says_it_was_salvaged(tmp_path) -> None:
    """wall_seconds is not recoverable after the fact, so the marker must
    not present an estimate as a measurement."""
    task_dir = task(tmp_path)
    run(task_dir)
    done = json.loads((task_dir / "DONE.json").read_text())
    assert done["salvaged"] is True
    assert done.get("wall_seconds") is None


def test_a_task_with_no_statistics_is_refused(tmp_path) -> None:
    task_dir = tmp_path / "task-000016"
    (task_dir / "shards").mkdir(parents=True)
    assert run(task_dir).returncode != 0


def test_several_tasks_can_be_salvaged_at_once(tmp_path) -> None:
    """Eight subjobs were rejected by one wave; salvaging them one command
    at a time is eight chances to mistype a path."""
    for task_id in (16, 17, 18):
        task(tmp_path, task_id)
    result = run(*[tmp_path / f"task-{i:06d}" for i in (16, 17, 18)])
    assert result.returncode == 0, result.stdout + result.stderr
    for task_id in (16, 17, 18):
        assert (tmp_path / f"task-{task_id:06d}" / "DONE.json").is_file()


def test_one_unhealthy_task_does_not_block_the_others(tmp_path) -> None:
    task(tmp_path, 16)
    task(tmp_path, 17, successes=1_000, unreachable=155_000, dns=706_000,
         per_shard=1_000_000)
    task(tmp_path, 18)
    result = run(*[tmp_path / f"task-{i:06d}" for i in (16, 17, 18)])
    assert result.returncode != 0, "the unhealthy one must be reported"
    assert (tmp_path / "task-000016" / "DONE.json").is_file()
    assert (tmp_path / "task-000018" / "DONE.json").is_file()
    assert not (tmp_path / "task-000017" / "DONE.json").exists()
