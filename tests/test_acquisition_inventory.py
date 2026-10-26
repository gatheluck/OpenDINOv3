"""Inventory distinguishes evidence from permission to reuse legacy shards."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/acquisition_inventory.py"


def run(root):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(root),
            "--first-task",
            "1",
            "--last-task",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def shard(root, task, nested=False, success=6):
    d = root / task
    if nested:
        d /= "shards"
    d.mkdir(parents=True)
    (d / "00000_stats.json").write_text(
        json.dumps(
            {
                "count": 10,
                "successes": success,
                "failed_to_download": 10 - success,
                "failed_to_resize": 0,
                "status_dict": {
                    "success": success,
                    "[Errno -2] Name or service not known": 10 - success,
                },
            }
        )
    )
    return d


def test_flat_nested_pilot_attempts_and_missing_tasks(tmp_path):
    shard(tmp_path, "task-000000")
    shard(tmp_path, "task-000001", nested=True, success=9)
    shard(tmp_path, "task-000001.attempt-old")
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["candidates"] == 10
    assert data["successes"] == 9
    assert data["missing_task_ids"] == [2]
    assert data["excluded_directories"] == ["task-000000", "task-000001.attempt-old"]
    assert data["tasks"][0]["healthy"] is True
    assert data["reuse_verified"] is False


def test_degraded_done_is_not_healthy(tmp_path):
    shard(tmp_path, "task-000001", success=0)
    (tmp_path / "task-000001/DONE.json").write_text("{}")
    data = json.loads(run(tmp_path).stdout)
    assert data["tasks"][0]["healthy"] is False
    assert data["tasks"][0]["done_marker_present"] is True
    assert data["reuse_verified"] is False


def test_corrupt_statistics_fail_closed(tmp_path):
    d = shard(tmp_path, "task-000001")
    (d / "00001_stats.json").write_text("{")
    result = run(tmp_path)
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert data["complete_scan"] is False
    assert data["errors"]


def test_invalid_arithmetic_fails_closed(tmp_path):
    d = shard(tmp_path, "task-000001")
    p = d / "00000_stats.json"
    s = json.loads(p.read_text())
    s["count"] = 11
    p.write_text(json.dumps(s))
    result = run(tmp_path)
    assert result.returncode == 2
    assert json.loads(result.stdout)["complete_scan"] is False


def test_missing_root_is_not_empty_success(tmp_path):
    assert run(tmp_path / "missing").returncode == 2
