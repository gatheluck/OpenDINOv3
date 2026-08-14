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


SUBMIT = SCRIPTS / "submit_production.sh"


def make_env(tmp_path, tasks: int = 8) -> dict:
    """Everything submit_production.sh needs, so a test exercises the guard
    under test rather than a missing prerequisite."""
    import os
    out = tmp_path / "out"
    (out / "logs").mkdir(parents=True)
    (out / "opendinov3.sif").write_bytes(b"")
    meta = tmp_path / "meta"
    meta.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "meta_dir": str(meta), "urls_per_task": 1, "total_rows": tasks,
        "tasks": [{"task_id": i, "rows": 1, "pieces": []}
                  for i in range(tasks)],
    }))
    return {**os.environ,
            "OD_SIF": str(out / "opendinov3.sif"), "OD_PLAN": str(plan),
            "OD_META_ROOT": str(meta), "OD_LOGDIR": str(out / "logs"),
            "OD_OUT_ROOT": str(out), "OD_TASK_ROOT": str(out / "tasks"),
            "OD_BLUR_FACES": "1"}


def submit(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SUBMIT), *map(str, args)],
                          capture_output=True, text=True, env=env)


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


def test_a_wave_cannot_be_submitted_without_stating_face_blurring(tmp_path
                                                                  ) -> None:
    """The blocker that would have wasted the whole pilot.

    production_task.sh refuses to run unless OD_BLUR_FACES is set — rightly,
    since blurring is irreversible across 902 million images. But the
    generated job script exported eight variables and this was not among
    them, and PBS does not forward the submitting shell's environment. So
    the wave would queue, wait, start, and every subjob would exit 2 on the
    first line.

    Stated at submit time, where a human is watching, and baked into the job.
    """
    env = make_env(tmp_path)
    env.pop("OD_BLUR_FACES", None)
    result = submit(env, "--from", "0", "--to", "7", "--dry-run")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    # Not merely the variable name: `set -u` produces "unbound variable"
    # further down, which names it too and would satisfy a loose check while
    # the guard was gone.
    assert "would fail on every node" in combined
    assert "unbound variable" not in combined


def test_the_stated_choice_is_baked_into_the_generated_job(tmp_path) -> None:
    env = make_env(tmp_path)
    env["OD_BLUR_FACES"] = "1"
    result = submit(env, "--from", "0", "--to", "7", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    job = Path(env["OD_LOGDIR"]) / "production_job.generated.sh"
    assert "export OD_BLUR_FACES=1" in job.read_text()


def test_an_invalid_blur_choice_is_refused_before_the_queue(tmp_path) -> None:
    env = make_env(tmp_path)
    env["OD_BLUR_FACES"] = "yes"
    result = submit(env, "--from", "0", "--to", "7", "--dry-run")
    assert result.returncode != 0


def test_the_submitted_array_starts_above_zero(tmp_path) -> None:
    """ABCI's qsub refuses index 0:

        qsub: Array job indices must be greater than 0.  [-J 0-7]

    Observed on 2026-08-14, after a plan and a wave had been prepared. PBS
    Pro accepts 0 elsewhere, so this is a site rule and the machine is the
    fact.
    """
    env = make_env(tmp_path)
    result = submit(env, "--from", "0", "--to", "7", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-J 1-8" in result.stdout, result.stdout


def test_the_offset_is_recorded_so_the_task_id_can_be_recovered(tmp_path
                                                                ) -> None:
    """Shifting the range without telling the job would run task 1 for
    array index 1 — every task off by one, every check still passing."""
    env = make_env(tmp_path)
    submit(env, "--from", "0", "--to", "7", "--dry-run")
    job = (Path(env["OD_LOGDIR"]) / "production_job.generated.sh").read_text()
    assert "export OD_TASK_ID_OFFSET=1" in job


@pytest.mark.parametrize("first,last,expected", [
    (0, 7, "-J 1-8"),
    (8, 15, "-J 9-16"),
    (1387, 1387, "-J 1388-1388"),
])
def test_the_shift_holds_across_the_range(tmp_path, first, last, expected
                                          ) -> None:
    """Including the last task of the real plan, where an off-by-one would
    silently drop the tail of the corpus."""
    env = make_env(tmp_path, tasks=1388)
    result = submit(env, "--from", str(first), "--to", str(last), "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
