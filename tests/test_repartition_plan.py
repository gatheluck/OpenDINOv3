"""CLI contracts: preserve plan lineage and never reinterpret written tasks."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/repartition_plan.py"


@pytest.fixture
def inputs(tmp_path):
    plan = {
        "meta_dir": "/metadata",
        "urls_per_task": 10,
        "total_rows": 23,
        "tasks": [
            {
                "task_id": i,
                "rows": end - start,
                "pieces": [{"path": "a.parquet", "start": start, "end": end}],
            }
            for i, (start, end) in enumerate([(0, 10), (10, 20), (20, 23)])
        ],
    }
    source = tmp_path / "old.json"
    source.write_text(json.dumps(plan))
    root = tmp_path / "tasks"
    root.mkdir()
    (root / "task-000000").mkdir()
    return source, tmp_path / "new.json", root


def run(inputs, size=4):
    source, output, root = inputs
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(source),
            str(output),
            "--first-task",
            "1",
            "--urls-per-task",
            str(size),
            "--task-root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_new_plan_preserves_prefix_total_and_source(inputs):
    source, output, _ = inputs
    before = source.read_bytes()
    result = run(inputs)
    assert result.returncode == 0, result.stderr
    assert output.exists(), "repartitioning must produce a usable plan"
    plan = json.loads(output.read_text())
    assert plan["tasks"][0] == json.loads(before)["tasks"][0]
    assert [t["rows"] for t in plan["tasks"]] == [10, 4, 4, 4, 1]
    assert plan["total_rows"] == 23
    assert plan["repartition"]["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert plan["repartition"]["first_task"] == 1
    assert plan["urls_per_task"] == 4
    assert source.read_bytes() == before
    assert run(inputs).returncode != 0  # never overwrite even on repetition


@pytest.mark.parametrize("name", ["task-000001", "task-000004"])
def test_any_existing_suffix_output_blocks_change(inputs, name):
    _, output, root = inputs
    (root / name).mkdir()
    assert run(inputs).returncode != 0
    assert not output.exists()


def test_existing_output_is_not_overwritten(inputs):
    _, output, _ = inputs
    output.write_text("original")
    assert run(inputs).returncode != 0
    assert output.read_text() == "original"


def test_missing_task_root_is_not_treated_as_empty(inputs):
    source, output, root = inputs
    assert run((source, output, root / "missing")).returncode != 0
    assert not output.exists()


def test_inconsistent_total_is_rejected(inputs):
    source, output, _ = inputs
    plan = json.loads(source.read_text())
    plan["total_rows"] += 1
    source.write_text(json.dumps(plan))
    assert run(inputs).returncode != 0
    assert not output.exists()


def test_real_manifest_extraction_preserves_all_rows(inputs):
    import os

    import pyarrow as pa
    import pyarrow.parquet as pq

    _source, output, root = inputs
    meta = root.parent / "metadata"
    meta.mkdir()
    pq.write_table(
        pa.table({"url": [f"https://example.test/{i}" for i in range(23)]}),
        meta / "a.parquet",
    )
    result = run(inputs)
    assert result.returncode == 0, result.stderr
    extracted = []
    for task in json.loads(output.read_text())["tasks"]:
        target = root.parent / f"manifest-{task['task_id']}.parquet"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT.parent / "build_task_manifest.py"),
                "--plan",
                str(output),
                "--task-id",
                str(task["task_id"]),
                "--output",
                str(target),
            ],
            env={**os.environ, "OD_META_ROOT": str(meta)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        extracted.extend(pq.read_table(target)["url"].to_pylist())
    assert extracted == [f"https://example.test/{i}" for i in range(23)]
