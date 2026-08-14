"""Rehearse the whole 0003 job script locally, end to end.

Experiment 0003 spent a queue slot and produced nothing: every phase ran,
every worker started, and not one shard was written. The cause was a staged
slice that did not resolve through the bind mount. No unit test could have
seen it, because every unit was correct — the job script itself had never
been executed.

So it is executed here: the real job script, the real slicer, the real
worker through img2dataset, the real analysis, against JPEGs served by a
local HTTP server. `tests/stubs/singularity` stands in for the container and
does the one thing that matters, translating bind paths.

Single node, because pbsdsh needs a cluster. The multi-node launch is the one
part this cannot rehearse, which is why the job runs the single-node phases
first.

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
STUBS = Path(__file__).resolve().parent / "stubs"
JOB = REPO / "scripts" / "experiment_0003_job.sh"

IMAGES = 60
SLICE = 12          # URLs per phase; 3 phases → 36 of the 60
TOTAL_PROCESSES = 2
SAMPLES_PER_SHARD = 2   # 6 shards per phase, 3 per process


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("images")
    for index in range(IMAGES):
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), (index * 4 % 256, 90, 160)).save(
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


def run_job(server, base, node_local=None):
    """Run the real job script against a tree rooted at `base`.

    `node_local` stands in for $PBS_LOCALDIR and must not sit under `base`.
    """
    node_local = node_local or (base.parent / f"{base.name}_nodelocal")
    urls_dir = base / "urls"
    urls_dir.mkdir(exist_ok=True)
    pq.write_table(
        pa.table({"url": [f"{server}/{i:04d}.jpg" for i in range(IMAGES)]}),
        urls_dir / "urls_clean.parquet",
    )
    exp_out = base / "out"
    exp_out.mkdir(exist_ok=True)

    env = {
        **os.environ,
        "PATH": f"{STUBS}:{os.environ['PATH']}",
        "OD_SIF": str(base / "fake.sif"),
        "OD_REPO": str(REPO),
        "OD_URLS": str(urls_dir / "urls_clean.parquet"),
        "OD_EXP_OUT": str(exp_out),
        "OD_SLICE": str(SLICE),
        "OD_TOTAL_PROCESSES": str(TOTAL_PROCESSES),
        "OD_NODES": "1",
        "OD_THREADS": "2",
        "OD_SAMPLES_PER_SHARD": str(SAMPLES_PER_SHARD),
        # Deliberately OUTSIDE the shared tree: $PBS_LOCALDIR is node-local,
        # and a test that puts it under the shared root would let a
        # node-local bind pass as shared storage. That is how the first
        # version of this test passed while the bug was present.
        "PBS_LOCALDIR": str(node_local),
    }
    (base / "fake.sif").write_bytes(b"")
    node_local.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(["bash", str(JOB)], capture_output=True, text=True,
                            env=env)
    return result, exp_out


@pytest.fixture(scope="module")
def job_run(server, tmp_path_factory):
    """Run the real job script once; every test reads its output."""
    return run_job(server, tmp_path_factory.mktemp("job0003"))


def test_the_job_reports_success_only_when_it_produced_something(job_run) -> None:
    """The previous run exited 0 having written nothing at all."""
    result, _ = job_run
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shards written: 0" not in result.stdout


def test_every_phase_wrote_shards(job_run) -> None:
    """The assertion that would have caught the staging bug.

    Each phase ran and each worker started last time too; only the shards
    were missing.
    """
    _, exp_out = job_run
    for phase in ("phase1_single", "phase2_multi", "phase3_single"):
        stats = list((exp_out / phase).rglob("*_stats.json"))
        assert stats, f"{phase} produced no shard"


def test_the_downloads_actually_succeeded(job_run) -> None:
    """Served locally, so any failure is ours rather than the internet's."""
    _, exp_out = job_run
    successes = attempted = 0
    for path in exp_out.rglob("*_stats.json"):
        stats = json.loads(path.read_text())
        successes += stats["successes"]
        attempted += stats["count"]
    assert attempted == SLICE * 3, f"attempted {attempted}, expected {SLICE * 3}"
    assert successes == attempted, f"only {successes}/{attempted} succeeded"


def test_the_phases_used_disjoint_urls(job_run) -> None:
    """Offsets exist so no phase warms remote caches for the next one."""
    _, exp_out = job_run
    seen: list[str] = []
    for name in ("slices_p1", "slices_p2", "slices_p3"):
        for slice_file in sorted((exp_out / name).glob("*.parquet")):
            seen += pq.read_table(slice_file).column("url").to_pylist()
    assert len(seen) == len(set(seen)), "phases share URLs"


def test_no_staged_slice_is_an_absolute_symlink(job_run) -> None:
    _, exp_out = job_run
    for path in exp_out.rglob("slice_1.parquet"):
        if path.is_symlink():
            assert not os.readlink(path).startswith("/"), path


def test_the_analysis_reads_the_job_output_back(job_run) -> None:
    _, exp_out = job_run
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "analyse_experiment_0003.py"),
         str(exp_out)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "pre-registered criteria" in result.stdout
    assert "NOT RUN" not in result.stdout, "every phase ran; none should be absent"


def test_a_second_run_into_the_same_directory_works_and_does_not_mix(
    server, tmp_path_factory
) -> None:
    """The failure mode of attempt 2: leftovers from attempt 1.

    Two things had to hold and neither did. Staging must overwrite what the
    last run left — `cp` refuses to write onto a symlink pointing at the
    source — and the analysis must not sum shards from two different runs
    and call the total one measurement.
    """
    base = tmp_path_factory.mktemp("job0003_rerun")

    first, exp_out = run_job(server, base)
    assert first.returncode == 0, first.stdout + first.stderr
    first_shards = len(list(exp_out.rglob("*_stats.json")))
    assert first_shards > 0

    second, exp_out_again = run_job(server, base)
    assert exp_out_again == exp_out
    assert second.returncode == 0, second.stdout + second.stderr
    assert "are the same file" not in second.stderr
    assert "moved to previous_" in second.stdout, \
        "the previous attempt should have been set aside"

    # The archive holds attempt 1; the live phase dirs hold only attempt 2.
    archived = list(exp_out.glob("previous_*"))
    assert archived, "nothing was archived"
    assert len(list(archived[0].rglob("*_stats.json"))) == first_shards

    live = [p for p in exp_out.glob("phase*") for _ in [0]]
    live_shards = sum(len(list(d.rglob("*_stats.json"))) for d in live)
    assert live_shards == first_shards, (
        f"the second run has {live_shards} shards but attempt 1 wrote "
        f"{first_shards}; runs are being mixed"
    )


def test_every_bind_source_is_shared_or_created_by_the_script(job_run) -> None:
    """The invariant that catches a node-local path without a second node.

    The scratch directory lives under $PBS_LOCALDIR, which ABCI's
    documentation states is node-local. The job created it on the node it ran
    on; every other node had none, so singularity failed to bind it and
    exited 255 before the worker started. node1 wrote nothing and the
    analysis compared half the work against the whole of it.

    No local test can allocate a second node. It does not need to: a bind
    source must either live on shared storage or be created by the script
    that binds it. That holds for one node and for a hundred, and it is
    checkable here.
    """
    result, exp_out = job_run
    scripts = sorted(exp_out.rglob("run.sh"))
    assert scripts, "no node script was generated"

    # The URL list and the repo are on shared storage; so is the experiment
    # output. Nothing else may be assumed to exist on another node.
    shared_roots = [str(REPO), str(exp_out), str(exp_out.parent / "urls")]

    for script in scripts:
        text = script.read_text()
        mkdirs = [line.split("mkdir -p", 1)[1].strip().strip('"')
                  for line in text.splitlines() if "mkdir -p" in line]
        exec_line = next(l for l in text.splitlines() if "singularity exec" in l)
        parts = exec_line.split()
        sources = [parts[i + 1].split(":")[0]
                   for i, part in enumerate(parts) if part == "--bind"]
        assert sources, f"{script} binds nothing"

        for source in sources:
            shared = any(source.startswith(root) for root in shared_roots)
            created = any(made.startswith(source) for made in mkdirs)
            assert shared or created, (
                f"{script.relative_to(exp_out)} binds {source}, which is "
                "neither on shared storage nor created by the script. On any "
                "node but the first it will not exist."
            )


def test_the_node_check_runs_before_any_phase(job_run) -> None:
    """Nodes are already allocated, so checking them costs seconds.

    Spending 35 minutes of phases on a node that cannot start the container
    is the expensive failure; this makes it a one-minute one.
    """
    result, exp_out = job_run
    out = result.stdout
    assert "node check" in out
    assert out.index("node check") < out.index("phase1_single"), \
        "the check must come before the phases, or it saves nothing"
    assert ": ok" in out
    assert (exp_out / "nodecheck" / "node0" / "probe.sh").is_file()


def test_the_node_check_uses_the_same_binds_as_the_real_phases(
    job_run,
) -> None:
    """A check that takes a different path checks nothing."""
    _, exp_out = job_run
    probe = (exp_out / "nodecheck" / "node0" / "probe.sh").read_text()
    run = next(exp_out.rglob("phase1_single/node0/run.sh")).read_text()

    def binds(text: str) -> set[str]:
        line = next(l for l in text.splitlines() if "singularity exec" in l)
        parts = line.split()
        return {parts[i + 1] for i, p in enumerate(parts) if p == "--bind"}

    assert binds(probe) == binds(run)
