"""Contract for the two commands a production subjob runs.

`build_task_manifest.py` turns one entry of the shared plan into the URL list
for that task. `assess_task.py` decides afterwards whether what came back is
worth keeping.

Both run inside the container on a compute node with no operator watching, so
each has to fail in a way the exit status carries. A subjob that returns 0
after writing nothing is exactly how 474 tasks were lost.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
BUILD = SCRIPTS / "build_task_manifest.py"
ASSESS = SCRIPTS / "assess_task.py"

DNS = "<urlopen error [Errno -2] Name or service not known>"
UNREACHABLE = "<urlopen error [Errno 101] Network is unreachable>"


def run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          capture_output=True, text=True)


@pytest.fixture
def corpus(tmp_path):
    """Two source files and a plan whose task 1 spans both."""
    meta = tmp_path / "meta"
    meta.mkdir()
    for name, first in (("a.parquet", 0), ("b.parquet", 100)):
        pq.write_table(pa.table({
            "url": [f"https://h.example/{first + i}.jpg" for i in range(100)],
            "uid": [f"{first + i:09d}" for i in range(100)],
        }), meta / name)

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": 60,
        "total_rows": 200,
        "tasks": [
            {"task_id": 0, "rows": 60,
             "pieces": [{"path": str(meta / "a.parquet"), "start": 0, "end": 60}]},
            {"task_id": 1, "rows": 60,
             "pieces": [{"path": str(meta / "a.parquet"), "start": 60, "end": 100},
                        {"path": str(meta / "b.parquet"), "start": 0, "end": 20}]},
        ],
    }))
    return plan, tmp_path


# --------------------------------------------------------------------------
# build_task_manifest.py
# --------------------------------------------------------------------------

def test_the_manifest_holds_exactly_the_planned_rows(corpus) -> None:
    plan, tmp = corpus
    out = tmp / "m.parquet"
    result = run(BUILD, "--plan", plan, "--task-id", 1, "--output", out)
    assert result.returncode == 0, result.stderr

    urls = pq.read_table(out).column("url").to_pylist()
    assert len(urls) == 60
    assert urls[0] == "https://h.example/60.jpg"
    assert urls[39] == "https://h.example/99.jpg"
    assert urls[40] == "https://h.example/100.jpg"


def test_a_task_id_outside_the_plan_is_refused(corpus) -> None:
    """An array index beyond the plan means the two disagree; guessing
    which is right would silently shorten the corpus."""
    plan, tmp = corpus
    result = run(BUILD, "--plan", plan, "--task-id", 99,
                 "--output", tmp / "m.parquet")
    assert result.returncode != 0
    assert "99" in result.stderr


def test_the_manifest_is_not_left_half_written_on_failure(corpus) -> None:
    """A truncated parquet would be read by the next attempt as a real
    manifest."""
    plan, tmp = corpus
    data = json.loads(plan.read_text())
    data["tasks"][0]["pieces"][0]["path"] = str(tmp / "gone.parquet")
    plan.write_text(json.dumps(data))

    out = tmp / "m.parquet"
    result = run(BUILD, "--plan", plan, "--task-id", 0, "--output", out)
    assert result.returncode != 0
    assert not out.exists()


def test_building_the_same_task_twice_gives_the_same_bytes(corpus) -> None:
    plan, tmp = corpus
    first, second = tmp / "1.parquet", tmp / "2.parquet"
    run(BUILD, "--plan", plan, "--task-id", 1, "--output", first)
    run(BUILD, "--plan", plan, "--task-id", 1, "--output", second)
    assert pq.read_table(first).equals(pq.read_table(second))


# --------------------------------------------------------------------------
# assess_task.py
# --------------------------------------------------------------------------

def write_shards(task_dir: Path, n: int, **status) -> None:
    shards = task_dir / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    total = sum(status.values())
    for i in range(n):
        (shards / f"{i:05d}_stats.json").write_text(json.dumps({
            "count": total, "successes": status.get("success", 0),
            "status_dict": dict(status)}))


def test_a_healthy_task_exits_zero_and_reports_its_yield(tmp_path) -> None:
    write_shards(tmp_path, 3, **{"success": 650, DNS: 60,
                                 "HTTP Error 404: Not Found": 290})
    result = run(ASSESS, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "65.0%" in result.stdout


def test_the_outage_profile_exits_nonzero_and_names_the_cause(tmp_path) -> None:
    """The 474-task case. A zero exit here is what lost them."""
    write_shards(tmp_path, 3, **{DNS: 706, UNREACHABLE: 155,
                                 "<urlopen error timed out>": 138, "success": 1})
    result = run(ASSESS, tmp_path)
    assert result.returncode != 0
    assert "unreachable" in (result.stdout + result.stderr).lower()


def test_a_task_with_no_statistics_at_all_is_refused(tmp_path) -> None:
    """img2dataset writing nothing is a failure, not a task of yield zero."""
    (tmp_path / "shards").mkdir()
    result = run(ASSESS, tmp_path)
    assert result.returncode != 0


def test_the_thresholds_can_be_tightened_but_are_reported(tmp_path) -> None:
    """Whatever they are, the run records what was applied."""
    write_shards(tmp_path, 2, **{"success": 400, "HTTP Error 404: Not Found": 600})
    ok = run(ASSESS, tmp_path)
    assert ok.returncode == 0
    strict = run(ASSESS, tmp_path, "--min-yield", "0.5")
    assert strict.returncode != 0
    assert "50" in strict.stdout + strict.stderr
