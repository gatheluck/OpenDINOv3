"""End-to-end run of the experiment pipeline against a real parquet list.

Every part of this was previously covered by unit tests that passed while the
pipeline as a whole could not have worked: the corpus stores parquet, and the
worker was passing `--input_format txt`. Unit tests cannot catch that, because
the mistake lives in the seam between them.

So this drives the real path: a parquet list shaped like the corpus's, cut by
the real slicer, downloaded by the real worker through img2dataset, and read
back by the real analysis. Images are served from a local HTTP server, so the
downloads genuinely succeed and the assertions are about real successes rather
than about everything failing in a plausible way.

Marked `integration`; it runs img2dataset and takes seconds rather than
milliseconds.
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
SCRIPTS = REPO / "scripts"
IMAGE_COUNT = 24
SLICE_SIZE = 8
LEVELS = "2 2"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server on port {port} never came up")


@pytest.fixture(scope="module")
def served_images(tmp_path_factory) -> tuple[str, Path]:
    """A local HTTP server holding real JPEGs.

    Real JPEGs, because img2dataset decodes what it fetches; a placeholder
    body would be counted as a download failure and the test would pass while
    measuring nothing.
    """
    root = tmp_path_factory.mktemp("images")
    for index in range(IMAGE_COUNT):
        buffer = io.BytesIO()
        Image.new("RGB", (64 + index, 48 + index), (index * 9 % 256, 120, 200)).save(
            buffer, format="JPEG"
        )
        (root / f"{index:04d}.jpg").write_bytes(buffer.getvalue())

    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(port)
        yield f"http://127.0.0.1:{port}", root
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture(scope="module")
def url_list(served_images, tmp_path_factory) -> Path:
    """A parquet list shaped like the corpus's `urls_clean.parquet`."""
    base, _ = served_images
    source = tmp_path_factory.mktemp("urls")
    table = pa.table({
        "key": [f"{i:09d}" for i in range(IMAGE_COUNT)],
        "url": [f"{base}/{i:04d}.jpg" for i in range(IMAGE_COUNT)],
        "width": [64 + i for i in range(IMAGE_COUNT)],
    })
    path = source / "urls_clean.parquet"
    pq.write_table(table, path)
    return path


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def test_inspect_reports_the_real_schema(url_list: Path) -> None:
    result = run([sys.executable, str(SCRIPTS / "slice_urls.py"),
                  str(url_list), "--inspect"])
    assert result.returncode == 0, result.stderr
    assert "format     : parquet" in result.stdout
    assert "url column : url" in result.stdout
    assert f"rows       : {IMAGE_COUNT}" in result.stdout


@pytest.fixture(scope="module")
def slices(url_list: Path, tmp_path_factory) -> Path:
    outdir = tmp_path_factory.mktemp("slices")
    result = run([sys.executable, str(SCRIPTS / "slice_urls.py"),
                  str(url_list), str(outdir),
                  "--count", str(SLICE_SIZE), "--slices", "2"])
    assert result.returncode == 0, result.stderr
    return outdir


def test_the_slicer_writes_parquet_and_records_the_column(slices: Path) -> None:
    """The worker reads the column name from here, so it must be written."""
    assert (slices / "slice_1.parquet").is_file()
    assert (slices / "slice_2.parquet").is_file()
    assert (slices / "url_column").read_text().strip() == "url"


def test_the_slices_are_disjoint(slices: Path) -> None:
    keys = []
    for name in ("slice_1.parquet", "slice_2.parquet"):
        keys += pq.read_table(slices / name).column("key").to_pylist()
    assert len(keys) == len(set(keys)) == 2 * SLICE_SIZE


@pytest.fixture(scope="module")
def experiment_output(slices: Path, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("expout")
    env = {
        **os.environ,
        "OD_SLICE_DIR": str(slices),
        "OD_EXP_OUT": str(out),
        "OD_LEVELS": LEVELS,
        "OD_THREADS": "4",
    }
    result = run(["bash", str(SCRIPTS / "experiment_0002_worker.sh")], env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "parquet" in result.stdout, "the worker should detect the parquet slices"
    return out


def test_the_worker_actually_downloaded_the_images(experiment_output: Path) -> None:
    """The assertion that would have caught the txt/parquet mismatch.

    With the wrong input format img2dataset still runs and still writes
    stats — every row simply fails. Only a non-zero success count
    distinguishes a working pipeline from a plausible-looking broken one.
    """
    stats_files = sorted(experiment_output.glob("run*/shards/*_stats.json"))
    assert stats_files, "no _stats.json written"

    total_successes = 0
    total_count = 0
    for path in stats_files:
        stats = json.loads(path.read_text())
        total_successes += stats["successes"]
        total_count += stats["count"]

    assert total_count == 2 * SLICE_SIZE, f"attempted {total_count} URLs"
    assert total_successes == total_count, (
        f"only {total_successes}/{total_count} downloads succeeded; "
        "the images were served locally, so any failure is ours"
    )


def test_both_levels_ran_and_recorded_their_wall_time(
    experiment_output: Path,
) -> None:
    runs = sorted(p.name for p in experiment_output.glob("run*_p*"))
    assert runs == ["run1_p2", "run2_p2"]
    for name in runs:
        wall = (experiment_output / name / "wall_seconds").read_text().strip()
        assert float(wall) >= 0


def test_the_analysis_reads_the_run_back(experiment_output: Path) -> None:
    result = run([sys.executable, str(SCRIPTS / "analyse_experiment_0002.py"),
                  str(experiment_output)])
    assert result.returncode == 0, result.stderr
    assert "pre-registered criteria" in result.stdout
    assert "100.0%" in result.stdout, "yield should be 100% against a local server"
    # Both levels used the same process count, so the drift control applies
    # and the scaling comparison has nothing to compare.
    assert "baseline drift" in result.stdout
