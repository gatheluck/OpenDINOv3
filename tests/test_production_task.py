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
        # Without this the fixture cannot exercise OD_BLUR_FACES=1 at all,
        # and the blur path is the one production actually runs.
        "face_bboxes": [[[0.1, 0.1, 0.5, 0.5]] for _ in range(IMAGES)],
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
    # It used to move the whole directory. Now only the empty shard goes,
    # so a killed task keeps the shards it did finish.
    assert "setting aside" in result.stdout

    assert list((task_root / "task-000000").glob("attempt-*")), \
        "the empty shard was lost instead of set aside as evidence"
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
        # Without this the fixture cannot exercise OD_BLUR_FACES=1 at all,
        # and the blur path is the one production actually runs.
        "face_bboxes": [[[0.1, 0.1, 0.5, 0.5]] for _ in range(IMAGES)],
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


def test_skipping_reencode_is_refused_while_blurring(workspace) -> None:
    """The combination that silently publishes unblurred faces.

    Pinned upstream by test_skip_reencode_silently_discards_face_blurring:
    with --resize_mode no, the blur is computed and then the original bytes
    are written. Every recorded field still looks correct, so nothing
    downstream would ever notice.
    """
    plan, task_root = workspace
    result = run_task(plan, task_root, blur="1", OD_SKIP_REENCODE="1")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "OD_SKIP_REENCODE" in combined
    assert "unblurred" in combined
    # It must stop HERE. Printing the warning and falling through to the
    # next guard would also exit non-zero with this message on screen, so
    # without this the test passes with the guard removed.
    assert "no face_bboxes column" not in combined, (
        "fell through to a later guard instead of stopping")


def test_blurring_does_not_duplicate_the_bbox_column(workspace) -> None:
    """img2dataset appends bbox_col to save_additional_columns itself, with
    no deduplication (main.py: `save_additional_columns.append(bbox_col)`).
    Carrying it as well makes every shard die with

        KeyError: 'Field "face_bboxes" exists 2 times in schema'

    after the download, so the task produces nothing at all. Found by
    scripts/rehearse_pilot.sh; missed here because the fixture had no
    face_bboxes column and so could never run the blur path.
    """
    import pyarrow.parquet as pq
    plan, task_root = workspace
    result = run_task(plan, task_root, blur="1")
    assert result.returncode == 0, result.stdout + result.stderr
    shard = sorted((task_root / "task-000000" / "shards").glob("*.parquet"))[0]
    names = pq.ParquetFile(shard).schema_arrow.names
    assert names.count("face_bboxes") == 1, names


def test_not_blurring_still_carries_the_boxes(workspace) -> None:
    """With blurring off there is no bbox_col, so the column has to be
    carried explicitly or blurring later needs a re-download."""
    import pyarrow.parquet as pq
    plan, task_root = workspace
    assert run_task(plan, task_root, blur="0").returncode == 0
    shard = sorted((task_root / "task-000000" / "shards").glob("*.parquet"))[0]
    assert "face_bboxes" in pq.ParquetFile(shard).schema_arrow.names


def test_the_fetch_settings_are_variable_and_recorded(workspace) -> None:
    """The first wave measured 34.9 URLs/sec/node against a model of 277
    while using 0.40% of the bandwidth and 0.53 of 192 cores, so per-request
    latency is the cost and these are the knobs that move it. An experiment
    comparing arms is worthless if each arm's settings are not recorded
    beside its result."""
    plan, task_root = workspace
    result = run_task(plan, task_root, OD_TIMEOUT="3", OD_RETRIES="0")
    assert result.returncode == 0, result.stdout + result.stderr
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["settings"]["timeout"] == 3
    assert done["settings"]["retries"] == 0
    # And that img2dataset was actually given them. DONE.json records the
    # intent; img2dataset.cmd records the run. Hard-coding a value back into
    # the call would leave DONE.json still reporting the variable, so an arm
    # would be credited to a setting it never used.
    argv = (task_root / "task-000000" / "img2dataset.cmd").read_text().split("\n")
    assert argv[argv.index("--timeout") + 1] == "3", argv
    assert argv[argv.index("--retries") + 1] == "0", argv


def test_the_manifest_can_be_capped_for_an_experiment(workspace) -> None:
    """An arm has to finish inside its walltime, or it measures how long a
    kill takes rather than how fast the setting is."""
    plan, task_root = workspace
    result = run_task(plan, task_root, OD_MAX_URLS="4")
    assert result.returncode == 0, result.stdout + result.stderr
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["candidates"] == 4, done


def test_without_a_cap_the_whole_task_is_fetched(workspace) -> None:
    """The cap must not have been bought by shrinking every task."""
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["candidates"] == TASK_ROWS


def test_the_default_settings_are_the_ones_experiment_0004_chose(workspace
                                                                  ) -> None:
    """retries=0 was measured, not reasoned: 3.31x the throughput of
    retries=2 for 0.2 points of yield, and less than half the unreachable
    rate. Reverting the default silently would undo a wave's worth of
    evidence, so it is pinned."""
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    settings = json.loads(
        (task_root / "task-000000" / "DONE.json").read_text())["settings"]
    assert settings["timeout"] == 10 and settings["retries"] == 0


def test_a_capped_task_is_not_recorded_as_complete(workspace) -> None:
    """Experiment 0004 ran tasks 8-11 with OD_MAX_URLS=100000 against a plan
    that allots each of them 1,000,000 URLs. They wrote DONE.json anyway, so
    every later wave skips them and 2.7 million URLs are permanently and
    silently missing — 0.195% of the corpus, marked complete.

    The measurement still has to be recorded, so DONE.json is still written;
    it just has to say what it is.
    """
    plan, task_root = workspace
    assert run_task(plan, task_root, OD_MAX_URLS="4").returncode == 0
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["partial"] is True
    assert done["candidates"] == 4
    assert done["planned_candidates"] == TASK_ROWS


def test_a_partial_task_is_redone_rather_than_skipped(workspace) -> None:
    """The skip exists so a requeued subjob does not re-download good data.
    A task holding a tenth of its URLs is not good data."""
    plan, task_root = workspace
    assert run_task(plan, task_root, OD_MAX_URLS="4").returncode == 0
    again = run_task(plan, task_root, attempt="retry")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already complete" not in again.stdout
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["candidates"] == TASK_ROWS
    assert done.get("partial") is False


def test_a_complete_task_is_still_skipped(workspace) -> None:
    """The guard must not have been bought by re-downloading everything."""
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    again = run_task(plan, task_root, attempt="retry")
    assert "already complete" in again.stdout


def test_a_marker_short_of_the_plan_is_redone_even_without_a_flag(workspace
                                                                  ) -> None:
    """Tasks 8, 9 and 10 on the cluster carry a DONE.json written before the
    partial flag existed: 100,000 candidates against a plan that allots them
    1,000,000. Keyed on the flag, they are skipped forever and 2.7 million
    URLs stay missing.

    Comparing the recorded candidates against the plan's row count needs no
    flag and repairs markers written by any earlier version.
    """
    plan, task_root = workspace
    task_dir = task_root / "task-000000"
    task_dir.mkdir(parents=True)
    (task_dir / "DONE.json").write_text(json.dumps({
        "task_id": 0, "candidates": TASK_ROWS // 4, "successes": 2}))
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already complete" not in result.stdout
    done = json.loads((task_dir / "DONE.json").read_text())
    assert done["candidates"] == TASK_ROWS


def test_a_marker_matching_the_plan_is_still_skipped(workspace) -> None:
    """The guard must not have been bought by redoing everything."""
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    again = run_task(plan, task_root, attempt="second")
    assert "already complete" in again.stdout


def test_a_marker_that_cannot_be_verified_is_redone(workspace) -> None:
    """An old marker with no candidates field cannot be checked against the
    plan. Resumption makes the conservative choice nearly free: the finished
    shards are kept, nothing is re-downloaded, and the marker is rewritten
    with the numbers to verify next time."""
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    task_dir = task_root / "task-000000"
    (task_dir / "DONE.json").write_text(json.dumps({"task_id": 0}))
    again = run_task(plan, task_root, attempt="unverifiable")
    assert "already complete" not in again.stdout
    assert json.loads((task_dir / "DONE.json").read_text())["candidates"] \
        == TASK_ROWS

def test_a_retry_resumes_instead_of_restarting(workspace) -> None:
    """The behaviour this pipeline was missing.

    A task killed at the walltime with most shards finished used to
    re-download all of them, because the runner moved the whole directory
    aside before every retry — while passing --incremental_mode
    incremental, whose entire job is to skip finished shards.

    Here the first two shards are already present and healthy. The retry
    must leave them alone and fetch only the rest.
    """
    import shutil
    plan, task_root = workspace
    assert run_task(plan, task_root).returncode == 0
    finished = task_root / "task-000000"
    keep = {p.name: p.read_bytes()
            for p in (finished / "shards").glob("00000*")}
    assert keep, "the fixture produced no first shard"

    # Undo completion, leaving the shards in place as a kill would.
    (finished / "DONE.json").unlink()

    again = run_task(plan, task_root, attempt="resumed")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "keeping" in again.stdout, again.stdout
    for name, before in keep.items():
        after = (finished / "shards" / name)
        assert after.is_file(), f"{name} was re-downloaded, not kept"
        assert after.read_bytes() == before, f"{name} was rewritten"


def test_a_retry_after_an_outage_does_not_inherit_the_empty_shards(workspace
                                                                   ) -> None:
    """The hazard the wholesale move was protecting against, kept."""
    plan, task_root = workspace
    shards = task_root / "task-000000" / "shards"
    shards.mkdir(parents=True)
    (shards / "00000.tar").write_bytes(b"empty")
    (shards / "00000_stats.json").write_text(json.dumps({
        "count": 4, "successes": 0,
        "status_dict": {"<urlopen error [Errno 101] Network is unreachable>": 4},
    }))
    result = run_task(plan, task_root, attempt="after-outage")
    assert result.returncode == 0, result.stdout + result.stderr
    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["successes"] == TASK_ROWS, "the empty shard was inherited"


def test_the_pooled_downloader_runs_the_task_and_says_it_did(workspace
                                                              ) -> None:
    """Reuse must produce the same corpus, and leave a record that it was on.

    DONE.json says what was intended; img2dataset.cmd says what ran. Which
    downloader fetched a task changes its throughput completely, so a rate
    measured later cannot be attributed without it.
    """
    plan, task_root = workspace
    result = run_task(plan, task_root, OD_HTTP_POOL="1")
    assert result.returncode == 0, result.stdout + result.stderr

    task_dir = task_root / "task-000000"
    done = json.loads((task_dir / "DONE.json").read_text())
    assert done["successes"] == TASK_ROWS, result.stdout + result.stderr

    recorded = (task_dir / "img2dataset.cmd").read_text()
    assert "img2dataset_pooled.py" in recorded, recorded


def test_the_upstream_downloader_stays_the_default(workspace) -> None:
    """Off unless asked for. The pooled path is new; the other has fetched
    every image in the corpus so far."""
    plan, task_root = workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr

    recorded = (task_root / "task-000000" / "img2dataset.cmd").read_text()
    assert "img2dataset_pooled.py" not in recorded, recorded


# --------------------------------------------------------------------------
# A corpus that is not DataComp
#
# dataset_schema and task_manifest are tested against COYO's spellings, but
# only as units. The runner was written against DataComp and names `url`,
# `text`, `uid` and `face_bboxes` directly, on the strength of the manifest
# renaming every corpus to those. That renaming is the seam, and COYO is the
# next corpus in scope, so it is driven here rather than assumed.
# --------------------------------------------------------------------------

@pytest.fixture
def coyo_workspace(server, tmp_path):
    """A plan over a COYO-shaped source: `id`, no boxes, a face *count*."""
    meta = tmp_path / "meta"
    meta.mkdir()
    source = meta / "a.parquet"
    pq.write_table(pa.table({
        "id": [i for i in range(IMAGES)],
        "url": [f"{server}/{i:04d}.jpg" for i in range(IMAGES)],
        "text": [f"caption {i}" for i in range(IMAGES)],
        "width": [64 for _ in range(IMAGES)],
        "height": [64 for _ in range(IMAGES)],
        # A count, not boxes. It must not be mistaken for something blurrable
        # and must not be carried as if it were.
        "num_faces": [2 for _ in range(IMAGES)],
        # COYO ships scores; they inflate the manifest and are recoverable
        # upstream by identifier, so they must be left behind.
        "clip_similarity_vitb32": [0.3 for _ in range(IMAGES)],
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


def test_a_coyo_shaped_corpus_runs_end_to_end(coyo_workspace) -> None:
    """`id` is not `uid` and there are no boxes; the run must still complete.

    Asserted against DONE.json rather than the exit code alone: the runner
    writes the marker only after the health check accepts the task, so a run
    that downloaded nothing would not produce one.
    """
    plan, task_root = coyo_workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr

    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["successes"] == TASK_ROWS, result.stdout + result.stderr


def test_coyos_identifier_survives_into_the_shard(coyo_workspace) -> None:
    """The whole point of the renaming.

    Checked in the shard's parquet, not the log: the "manifest columns" line
    prints `uid` whether or not img2dataset was ever told to carry it.
    """
    plan, task_root = coyo_workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr

    shard = sorted((task_root / "task-000000" / "shards").glob("*.parquet"))[0]
    names = pq.ParquetFile(shard).schema_arrow.names
    assert "uid" in names, f"COYO's `id` was dropped: {names}"
    assert "num_faces" not in names, f"a face count was carried: {names}"
    assert "clip_similarity_vitb32" not in names, names


def test_blurring_a_corpus_without_boxes_is_refused(coyo_workspace) -> None:
    """COYO records how many faces, never where.

    The failure that matters is the quiet one: a wave asked to blur, running
    to completion, and publishing unblurred faces. It must stop instead, and
    before spending a node-hour.

    Pinned here rather than once per corpus: the guard keys off the manifest's
    columns, not off which corpus produced them, so Re-LAION takes this same
    branch and a second copy would cost a download without adding coverage.
    """
    plan, task_root = coyo_workspace
    result = run_task(plan, task_root, blur="1")
    assert result.returncode != 0, result.stdout + result.stderr

    combined = result.stdout + result.stderr
    assert "face_bboxes" in combined, combined
    assert not (task_root / "task-000000" / "DONE.json").exists()


# --------------------------------------------------------------------------
# Re-LAION — the same seam, with every name in the wrong case
#
# LAION spells all five roles differently from DataComp, and its own card
# warns that naming is not uniform across their repositories. The runner
# names `url` and `text` lowercase, so the whole corpus rests on the manifest
# lowercasing them first. Nothing downstream would report the omission: a
# caption column that was never bound looks exactly like a corpus with no
# captions.
# --------------------------------------------------------------------------

@pytest.fixture
def relaion_workspace(server, tmp_path):
    """A plan over a Re-LAION-shaped source: uppercase names, `hash`, no boxes."""
    meta = tmp_path / "meta"
    meta.mkdir()
    source = meta / "a.parquet"
    pq.write_table(pa.table({
        "URL": [f"{server}/{i:04d}.jpg" for i in range(IMAGES)],
        "TEXT": [f"caption {i}" for i in range(IMAGES)],
        "WIDTH": [64 for _ in range(IMAGES)],
        "HEIGHT": [64 for _ in range(IMAGES)],
        "hash": [f"{i:016x}" for i in range(IMAGES)],
        "similarity": [0.3 for _ in range(IMAGES)],
        "punsafe": [0.01 for _ in range(IMAGES)],
        "pwatermark": [0.02 for _ in range(IMAGES)],
        "LANGUAGE": ["en" for _ in range(IMAGES)],
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


def test_a_relaion_shaped_corpus_runs_end_to_end(relaion_workspace) -> None:
    """`URL` is not `url`. The run must still fetch every image."""
    plan, task_root = relaion_workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr

    done = json.loads((task_root / "task-000000" / "DONE.json").read_text())
    assert done["successes"] == TASK_ROWS, result.stdout + result.stderr


def test_relaions_caption_and_identifier_survive_the_case_change(
        relaion_workspace) -> None:
    """A dropped caption is invisible downstream, so it is checked here.

    Read from the shard's own parquet: img2dataset writes `caption` only when
    it was given a caption column, so its presence is evidence the binding
    happened rather than evidence of what the log claimed. The value is
    checked too — the column exists and holds null for every row when the
    binding was made against a name the manifest does not carry.
    """
    plan, task_root = relaion_workspace
    result = run_task(plan, task_root)
    assert result.returncode == 0, result.stdout + result.stderr

    shard = sorted((task_root / "task-000000" / "shards").glob("*.parquet"))[0]
    table = pq.read_table(shard)
    names = table.schema.names

    assert "uid" in names, f"Re-LAION's `hash` was dropped: {names}"
    assert "caption" in names, f"TEXT was never bound as the caption: {names}"
    assert table.column("caption")[0].as_py().startswith("caption "), (
        f"the caption column is present but empty: "
        f"{table.column('caption')[0].as_py()!r}")
    # Scores and language are recoverable upstream by identifier; carrying
    # them would inflate every shard for the whole corpus.
    for dropped in ("similarity", "punsafe", "pwatermark", "LANGUAGE"):
        assert dropped not in names, f"{dropped} was carried: {names}"
    # img2dataset writes the real decoded size itself, exactly once.
    assert names.count("width") == 1, names
