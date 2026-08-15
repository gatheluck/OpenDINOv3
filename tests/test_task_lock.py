"""Contract for a task having exactly one owner at a time.

Re-submitting a range is already safe when nothing is in flight: a task with
DONE.json is skipped, and one without is redone. What is NOT safe is the
same task running twice at once. The second subjob treats the first's live
output as wreckage, moves it aside mid-write, and both then write to the
same directory.

That made correctness depend on the operator remembering which ranges were
still running — across days, 1,388 tasks, and an ABCI that can stop. It is
the wrong thing to depend on.

With a lock, `--from 0 --to 1387` is always the right command: complete
tasks skip, tasks owned by a live subjob are left alone, and everything
else is redone.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "scripts" / "production_task.sh"

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(tmp_path):
    """A plan with one task whose URLs point nowhere, so the download is
    quick and the health check rejects it. The lock behaviour is what is
    under test, not the download."""
    meta = tmp_path / "meta"
    meta.mkdir()
    source = meta / "a.parquet"
    pq.write_table(pa.table({
        "url": [f"http://127.0.0.1:1/{i}.jpg" for i in range(4)],
        "uid": [f"{i:09d}" for i in range(4)],
    }), source)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "meta_dir": str(meta), "urls_per_task": 4, "total_rows": 4,
        "tasks": [{"task_id": 0, "rows": 4, "pieces": [
            {"path": "a.parquet", "start": 0, "end": 4}]}],
    }))
    return plan, tmp_path / "tasks", meta


def run_task(plan, task_root, meta, **extra):
    env = {**os.environ, "OD_PLAN": str(plan), "OD_TASK_ID": "0",
           "OD_TASK_ROOT": str(task_root), "OD_META_ROOT": str(meta),
           "OD_PROCESSES": "1", "OD_THREADS": "1",
           "OD_SAMPLES_PER_SHARD": "4", "OD_BLUR_FACES": "0",
           "OD_TIMEOUT": "1", "OD_RETRIES": "0", **extra}
    return subprocess.run(["bash", str(RUNNER)], capture_output=True,
                          text=True, env=env)


def held_lock(task_root: Path, *, age_seconds: int = 0) -> Path:
    """A lock as a live subjob would leave it."""
    lock = task_root / "task-000000" / "RUNNING.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(json.dumps(
        {"job": "9999[1].pbs1", "host": "node042", "pid": 1}))
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(lock / "owner", (old, old))
        os.utime(lock, (old, old))
    return lock


def test_a_second_subjob_leaves_a_running_task_alone(workspace) -> None:
    """The corruption case. The output of the live subjob must still be
    there afterwards, not moved aside."""
    plan, task_root, meta = workspace
    held_lock(task_root)
    canary = task_root / "task-000000" / "shards"
    canary.mkdir(parents=True, exist_ok=True)
    (canary / "00000.tar").write_bytes(b"live output")

    result = run_task(plan, task_root, meta)
    assert "another subjob" in (result.stdout + result.stderr).lower()
    assert (canary / "00000.tar").read_bytes() == b"live output"
    assert not list(task_root.glob("task-000000.attempt-*")), \
        "the live subjob's output was set aside"


def test_a_duplicate_does_not_fail_the_array(workspace) -> None:
    """Someone else owning the task is not an error. Exiting non-zero would
    fill the wave's logs with failures for tasks that are being handled."""
    plan, task_root, meta = workspace
    held_lock(task_root)
    assert run_task(plan, task_root, meta).returncode == 0


def test_a_stale_lock_is_taken_over(workspace) -> None:
    """ABCI stops, the node dies, the lock outlives its owner. Refusing
    forever would strand the task."""
    plan, task_root, meta = workspace
    held_lock(task_root, age_seconds=3600)
    result = run_task(plan, task_root, meta, OD_LOCK_STALE_SECONDS="600")
    combined = result.stdout + result.stderr
    # NOT a substring of the output: pytest's tmp_path embeds this test's
    # own name, and "stale" is in it, so `"stale" in combined` passed with
    # the takeover removed entirely. Assert that the run got PAST the lock.
    assert (task_root / "task-000000" / "urls.parquet").is_file(), combined
    assert "taking over" in combined, combined
    assert "another subjob owns" not in combined


def test_the_lock_is_released_when_the_task_fails(workspace) -> None:
    """These URLs go nowhere, so the health check rejects the task. A lock
    left behind would strand it until it went stale."""
    plan, task_root, meta = workspace
    result = run_task(plan, task_root, meta)
    assert result.returncode != 0, "the fixture is meant to fail"
    assert not (task_root / "task-000000" / "RUNNING.lock").exists()


def test_the_lock_names_its_owner(workspace) -> None:
    """A stuck task should be traceable to the job and node holding it."""
    plan, task_root, meta = workspace
    held_lock(task_root)
    result = run_task(plan, task_root, meta)
    assert "9999[1].pbs1" in result.stdout + result.stderr
    assert "node042" in result.stdout + result.stderr


def test_a_completed_task_is_still_skipped_without_taking_a_lock(workspace
                                                                 ) -> None:
    plan, task_root, meta = workspace
    task_dir = task_root / "task-000000"
    task_dir.mkdir(parents=True)
    (task_dir / "DONE.json").write_text(json.dumps({"task_id": 0}))
    result = run_task(plan, task_root, meta)
    assert result.returncode == 0
    assert "already complete" in result.stdout
    assert not (task_dir / "RUNNING.lock").exists()
