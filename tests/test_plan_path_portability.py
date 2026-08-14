"""The plan must survive being read under a different mount layout.

plan_partition.py records the absolute path of every parquet file it read.
od.sh runs it inside the container, where the corpus is bound at /corpus, so
the plan comes out holding /corpus/... paths.

production_job.sh binds the metadata at its HOST path
(`--bind "${OD_META_ROOT}:${OD_META_ROOT}:ro"`) and does not bind /corpus at
all. So the manifest builder opens a path that does not exist in its
container — after the queue wait, on every subjob.

The stub in tests/stubs/singularity cannot catch this: it rewrites bind
paths and runs the command directly, so it does not isolate the filesystem.
That limitation is documented there, and this is the bug it lets through.

A plan is a description of the data, not of one machine's mount table.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "scripts" / "build_task_manifest.py"


def make_source(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({
        "url": [f"https://x/{i}.jpg" for i in range(rows)],
        "text": [f"caption {i}" for i in range(rows)],
        "uid": [f"{i:09d}" for i in range(rows)],
    }), path)


def build(plan: Path, out: Path, task_id: int = 0, **env_extra):
    import os
    return subprocess.run(
        [sys.executable, str(BUILD), "--plan", str(plan),
         "--task-id", str(task_id), "--output", str(out)],
        capture_output=True, text=True, env={**os.environ, **env_extra})


def test_a_plan_written_under_another_mount_still_resolves(tmp_path) -> None:
    """The exact production shape: the plan says /corpus/..., the job binds
    the host path. Rebasing on OD_META_ROOT is what makes the plan portable.
    """
    real = tmp_path / "host" / "upstream_metadata"
    make_source(real / "part-00000.parquet", 8)

    # A plan as od.sh would write it: container-side paths that do not exist
    # anywhere in this process's filesystem.
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": 8, "total_rows": 8,
        "tasks": [{"task_id": 0, "rows": 8, "pieces": [
            {"path": "/corpus/datacomp/datacomp_1b/upstream_metadata/"
                     "part-00000.parquet", "start": 0, "end": 8}]}],
    }))

    out = tmp_path / "urls.parquet"
    result = build(plan, out, OD_META_ROOT=str(real))
    assert result.returncode == 0, result.stdout + result.stderr
    assert pq.ParquetFile(out).metadata.num_rows == 8


def test_an_unresolvable_piece_names_the_variable_that_would_fix_it(tmp_path
                                                                    ) -> None:
    """Failing is fine. Failing without saying which knob to turn is not:
    this surfaces on a compute node, in a subjob log, after a queue wait."""
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": 8, "total_rows": 8,
        "tasks": [{"task_id": 0, "rows": 8, "pieces": [
            {"path": "/corpus/nowhere/part-00000.parquet",
             "start": 0, "end": 8}]}],
    }))
    result = build(plan, tmp_path / "urls.parquet",
                   OD_META_ROOT=str(tmp_path / "empty"))
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "OD_META_ROOT" in combined
    assert "/corpus/nowhere/part-00000.parquet" in combined
    # And which tree was actually searched — without it the operator cannot
    # tell "wrong variable" from "right variable, wrong corpus".
    assert str(tmp_path / "empty") in combined


def test_a_path_that_exists_is_used_as_is(tmp_path) -> None:
    """Rebasing must not break the ordinary case where the plan and the
    filesystem already agree."""
    real = tmp_path / "meta"
    make_source(real / "part-00000.parquet", 8)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": 8, "total_rows": 8,
        "tasks": [{"task_id": 0, "rows": 8, "pieces": [
            {"path": str(real / "part-00000.parquet"), "start": 0,
             "end": 8}]}],
    }))
    out = tmp_path / "urls.parquet"
    assert build(plan, out).returncode == 0
    assert pq.ParquetFile(out).metadata.num_rows == 8


def test_rebasing_matches_on_more_than_the_basename(tmp_path) -> None:
    """Two shards can share a basename across subdirectories. Matching on
    the last component alone would silently pair the wrong file, and the
    task would download someone else's URLs while looking healthy."""
    real = tmp_path / "meta"
    make_source(real / "a" / "part-00000.parquet", 8)
    make_source(real / "b" / "part-00000.parquet", 8)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": 8, "total_rows": 8,
        "tasks": [{"task_id": 0, "rows": 8, "pieces": [
            {"path": "/corpus/meta/b/part-00000.parquet", "start": 0,
             "end": 8}]}],
    }))
    out = tmp_path / "urls.parquet"
    result = build(plan, out, OD_META_ROOT=str(real))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "b/part-00000.parquet" in result.stdout


def test_a_shorter_match_does_not_beat_a_longer_one(tmp_path) -> None:
    """The trap the longest-suffix rule exists for.

    If the root holds both `part-00000.parquet` and `b/part-00000.parquet`,
    trying short suffixes first pairs the plan's `b/part-00000.parquet` with
    the top-level file. Both are valid parquet with the right row count, so
    every check downstream passes and the task downloads the wrong URLs.
    """
    real = tmp_path / "meta"
    make_source(real / "part-00000.parquet", 8)      # decoy at the top
    make_source(real / "b" / "part-00000.parquet", 8)
    # Make them distinguishable: the decoy gets different URLs.
    pq.write_table(pa.table({
        "url": [f"https://DECOY/{i}.jpg" for i in range(8)],
        "text": [f"caption {i}" for i in range(8)],
        "uid": [f"{i:09d}" for i in range(8)],
    }), real / "part-00000.parquet")

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": 8, "total_rows": 8,
        "tasks": [{"task_id": 0, "rows": 8, "pieces": [
            {"path": "/corpus/meta/b/part-00000.parquet", "start": 0,
             "end": 8}]}],
    }))
    out = tmp_path / "urls.parquet"
    result = build(plan, out, OD_META_ROOT=str(real))
    assert result.returncode == 0, result.stdout + result.stderr
    urls = pq.read_table(out).column("url").to_pylist()
    assert not any("DECOY" in u for u in urls), "paired with the wrong shard"
