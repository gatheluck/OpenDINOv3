"""Run the real production task end to end, against a real download.

The four bugs that cost queue slots in experiment 0003 all lived in the seam
between correct components, and none was visible to a unit test. This drives
the actual runner: the real manifest builder, real img2dataset, real health
check, real DONE marker — against JPEGs served locally so the downloads
genuinely succeed.

The cases that matter are the ones production will actually hit: a requeued
subjob finding its task already done, and a retry finding the wreckage of a
failed attempt.

Marked `integration`.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "scripts" / "production_task.sh"
IMAGES = 40
TASK_ROWS = 16


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("images")
    for index in range(IMAGES):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), (index * 6 % 256, 100, 150)).save(
            buffer, format="JPEG")
        (root / f"{index:04d}.jpg").write_bytes(buffer.getvalue())

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def workspace(server, tmp_path):
    """A plan over one source file, cut into tasks of TASK_ROWS URLs."""
    meta = tmp_path / "meta"
    meta.mkdir()
    source = meta / "a.parquet"
    pq.write_table(pa.table({
        "url": [f"{server}/{i:04d}.jpg" for i in range(IMAGES)],
        "text": [f"caption {i}" for i in range(IMAGES)],
        "uid": [f"{i:09d}" for i in range(IMAGES)],
    }), source)

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "urls_per_task": TASK_ROWS,
        "total_rows": IMAGES,
        "tasks": [
            {"task_id": t, "rows": TASK_ROWS,
             "pieces": [{"path": str(source),
                         "start": t * TASK_ROWS,
                         "end": (t + 1) * TASK_ROWS}]}
            for t in range(IMAGES // TASK_ROWS)
        ],
    }))
    return plan, tmp_path / "tasks"


def run_task(plan: Path, task_root: Path, task_id: int = 0, **extra):
    env = {
        **os.environ,
        "OD_PLAN": str(plan),
        "OD_TASK_ID": str(task_id),
        "OD_TASK_ROOT": str(task_root),
        "OD_PROCESSES": "2",
        "OD_THREADS": "2",
        "OD_SAMPLES_PER_SHARD": "4",
        "OD_ATTEMPT_TAG": extra.pop("attempt", "test"),
        "OD_BLUR_FACES": extra.pop("blur", "0"),
        **extra,
    }
    return subprocess.run(["bash", str(RUNNER)], capture_output=True,
                          text=True, env=env)


def test_a_task_downloads_and_is_marked_done(workspace) -> None:
    plan, task_root = workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr

    task_dir = task_root / "task-000000"
    done = json.loads((task_dir / "DONE.json").read_text())
    assert done["task_id"] == 0
    assert done["candidates"] == TASK_ROWS
    assert done["successes"] == TASK_ROWS, "served locally; all should succeed"
    assert done["settings"]["samples_per_shard"] == 4


def test_the_shards_and_the_manifest_are_where_the_corpus_expects(
    workspace,
) -> None:
    plan, task_root = workspace
    run_task(plan, task_root)
    task_dir = task_root / "task-000000"
    assert (task_dir / "urls.parquet").is_file()
    assert sorted(p.name for p in (task_dir / "shards").glob("*.tar")) == [
        "00000.tar", "00001.tar", "00002.tar", "00003.tar"]


def test_a_requeued_subjob_does_not_download_again(workspace) -> None:
    """Waves get resubmitted and PBS requeues subjobs. Re-downloading would
    waste a node-hour and replace good data with whatever the web returns
    today."""
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    first = (task_root / "task-000000" / "DONE.json").read_text()

    again = run_task(plan, task_root)
    assert again.returncode == 0
    assert "already complete" in again.stdout
    assert (task_root / "task-000000" / "DONE.json").read_text() == first


def test_a_failed_attempt_is_set_aside_rather_than_added_to(workspace) -> None:
    """The trap experiment 0003 hit twice.

    img2dataset skips any shard that already has output, and a failed attempt
    leaves statistics behind even when it stored nothing. Left in place, the
    retry skips everything and reproduces the empty task.
    """
    plan, task_root = workspace
    wreckage = task_root / "task-000000" / "shards"
    wreckage.mkdir(parents=True)
    (wreckage / "00000_stats.json").write_text(json.dumps({
        "count": 4, "successes": 0,
        "status_dict": {"<urlopen error [Errno 101] Network is unreachable>": 4},
    }))

    result = run_task(plan, task_root, attempt="retry")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "moving it to" in result.stdout

    assert list(task_root.glob("task-000000.attempt-*")), "wreckage was lost"
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["successes"] == TASK_ROWS, "the retry inherited the empty shard"


def test_an_unhealthy_task_is_not_marked_done(workspace) -> None:
    """A subjob that stores nothing must fail, not report success.

    Every URL points at a port with nothing behind it, which is the shape of
    the 2026-07-28 outage.
    """
    plan, task_root = workspace
    dead = json.loads(plan.read_text())
    dead_port = free_port()
    source = Path(dead["tasks"][0]["pieces"][0]["path"])
    pq.write_table(pa.table({
        "url": [f"http://127.0.0.1:{dead_port}/{i}.jpg" for i in range(IMAGES)],
        "uid": [f"{i:09d}" for i in range(IMAGES)],
    }), source)

    result = run_task(plan, task_root)
    assert result.returncode != 0
    task_dir = task_root / "task-000000"
    assert not (task_dir / "DONE.json").exists()
    assert (task_dir / "health.json").is_file(), "the verdict must be kept"


def test_the_run_refuses_until_face_blurring_is_chosen(workspace) -> None:
    """Irreversible, 902 million images, and a legal question.

    DataComp blurs by default. A default either way here would settle that
    by accident, so the run stops and says so.
    """
    plan, task_root = workspace
    env = {
        **os.environ,
        "OD_PLAN": str(plan), "OD_TASK_ID": "0",
        "OD_TASK_ROOT": str(task_root), "OD_PROCESSES": "2",
        "OD_THREADS": "2", "OD_SAMPLES_PER_SHARD": "4",
    }
    env.pop("OD_BLUR_FACES", None)
    result = subprocess.run(["bash", str(RUNNER)], capture_output=True,
                            text=True, env=env)
    assert result.returncode != 0
    assert "OD_BLUR_FACES is not set" in result.stderr
    assert not (task_root / "task-000000").exists(), "nothing should be written"


def test_an_invalid_blur_setting_is_refused(workspace) -> None:
    plan, task_root = workspace
    result = run_task(plan, task_root, blur="maybe")
    assert result.returncode != 0
    assert "must be 0 or 1" in result.stderr


def test_captions_reach_the_shards(workspace) -> None:
    """The whole point of carrying the text column.

    Without --caption_col the tar holds .jpg and .json only, and the corpus
    cannot train anything text-conditioned.
    """
    import tarfile
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    tar = sorted((task_root / "task-000000" / "shards").glob("*.tar"))[0]
    with tarfile.open(tar) as archive:
        names = archive.getnames()
        suffixes = {n.rsplit(".", 1)[-1] for n in names}
        assert "txt" in suffixes, f"no captions in the shard: {sorted(suffixes)}"
        caption = archive.extractfile(
            next(n for n in names if n.endswith(".txt"))).read().decode()
    assert caption.startswith("caption "), caption


def test_carrying_a_column_img2dataset_writes_itself_is_refused(workspace
                                                                ) -> None:
    """img2dataset appends `key`, `status`, `error_message`, `width`,
    `height`, `original_width` and `original_height` to the input schema.

    Carrying an upstream column of the same name puts two fields with one
    name in the output schema. The writer's buffer is `{k: [] for k in
    schema.names}`, so the duplicate collapses to one key that receives two
    appends per row while every other key receives one. The rows then
    misalign and pyarrow raises `Expected bytes, got a 'int' object` — at
    *write* time, meaning after a shard has been downloaded in full, on every
    shard, for the whole run.

    Carrying `width` is an obvious thing to want, so this refuses up front
    rather than 23 TB later.
    """
    plan, task_root = workspace
    result = run_task(plan, task_root, OD_CARRY_COLUMNS="uid width")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "width" in combined
    assert "img2dataset" in combined


def test_the_columns_carried_are_still_the_ones_we_want(workspace) -> None:
    """The guard must not have been bought by carrying nothing.

    Asserted against the shard's own parquet, not the log: `uid` appears in
    the "manifest columns" line whether or not it was ever carried, so a log
    check would pass for the wrong reason.
    """
    import pyarrow.parquet as pq
    plan, task_root = workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr
    shard = sorted((task_root / "task-000000" / "shards").glob("*.parquet"))[0]
    names = pq.ParquetFile(shard).schema_arrow.names
    assert "uid" in names, f"uid was not carried into the shard: {names}"
    # And what img2dataset writes itself is there exactly once.
    assert names.count("width") == 1, names
