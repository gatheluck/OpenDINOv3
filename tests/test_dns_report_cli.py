"""Reports expose observed API calls without claiming wire DNS coverage."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def task(root, number, log=None, marker=None):
    path = root / f"task-{number:06d}"
    path.mkdir()
    if log is not None:
        (path / "img2dataset.log").write_text(log)
    if marker is not None:
        (path / "DONE.json").write_text(json.dumps(marker))
    return path


def report(root, *args):
    output = root / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/dns_report.py"),
            str(root),
            "--json",
            str(output),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.is_file(), "unknown measurements must still be written as JSON"
    return json.loads(output.read_text()), result.stdout


def test_missing_reports_are_unknown_even_when_cache_was_enabled(tmp_path):
    task(
        tmp_path,
        0,
        "download interrupted\n",
        {
            "wall_seconds": 10,
            "settings": {"dns_cache": 1},
        },
    )
    data, stdout = report(tmp_path)
    row = data["tasks"][0]
    assert row["measurement_status"] == "unknown"
    assert row["observed_resolver_calls"] is None
    assert row["observed_resolver_calls_per_second"] is None
    assert "cache was off" not in stdout
    assert "None of these tasks ran with OD_DNS_CACHE=1" not in stdout


def test_api_counts_are_partial_observations_not_wire_queries(tmp_path):
    task(
        tmp_path,
        0,
        "OD_DNS_CACHE_STATS hits=90 misses=10\n",
        {
            "wall_seconds": 2,
        },
    )
    data, stdout = report(tmp_path)
    row = data["tasks"][0]
    assert row["observed_resolver_calls"] == 10
    assert row["observed_resolver_calls_per_second"] == 5
    assert row["observed_cache_hit_fraction"] == 0.9
    assert row["measurement_status"] == "partial"
    assert data["metric"] == "getaddrinfo_calls_after_cache_miss"
    assert data["rate_scope"] == "task_wall_time_average_of_observed_counts"
    assert data["coverage"] == "unverified"
    assert "queries" not in row
    assert "queries_per_second" not in row
    assert "queries sent" not in stdout
    assert "queries/sec/node" not in stdout
    assert "At N nodes" not in stdout


def test_mixed_and_unfinished_tasks_remain_visible(tmp_path):
    task(tmp_path, 0, "OD_DNS_CACHE_STATS hits=9 misses=1\n", {"wall_seconds": 2})
    task(tmp_path, 1, "OD_DNS_CACHE_STATS hits=3 misses=2\n")
    task(tmp_path, 2, marker={"wall_seconds": 3})
    data, _ = report(tmp_path)
    rows = {row["task"]: row for row in data["tasks"]}
    assert set(rows) == {"task-000000", "task-000001", "task-000002"}
    assert rows["task-000001"]["measurement_status"] == "partial"
    assert rows["task-000001"]["observed_resolver_calls"] == 2
    assert rows["task-000001"]["observed_resolver_calls_per_second"] is None
    assert rows["task-000002"]["measurement_status"] == "unknown"


@pytest.mark.parametrize("marker", [[], {"wall_seconds": -2}, {"wall_seconds": True}])
def test_invalid_wall_time_never_produces_a_rate(tmp_path, marker):
    task(tmp_path, 0, "OD_DNS_CACHE_STATS hits=1 misses=1\n", marker)
    data, _ = report(tmp_path)
    assert data["tasks"][0]["observed_resolver_calls_per_second"] is None


def test_zero_counts_are_distinct_from_missing_counts(tmp_path):
    task(tmp_path, 0, "OD_DNS_CACHE_STATS hits=0 misses=0\n", {"wall_seconds": 2})
    data, _ = report(tmp_path)
    row = data["tasks"][0]
    assert row["measurement_status"] == "partial"
    assert row["observed_resolver_calls"] == 0
    assert row["observed_resolver_calls_per_second"] == 0
    assert row["observed_cache_hit_fraction"] is None


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_task_limit_must_be_positive(tmp_path, limit):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/dns_report.py"),
            str(tmp_path),
            "--tasks",
            limit,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--tasks must be positive" in result.stderr
