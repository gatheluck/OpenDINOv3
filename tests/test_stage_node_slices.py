"""Staged slices must still resolve when the tree is mounted somewhere else.

This is the bug that cost experiment 0003 a queue slot. The job staged each
node's slice with

    ln -sf "${OD_EXP_OUT}/slices_p1/slice_1.parquet" "${dir}/slices/slice_1.parquet"

which writes an absolute host path into the link. The worker then reads it
through a bind mount, where that path does not exist, so the file it was
looking at was simply not there:

    ❌ no slice_1.parquet or slice_1.txt in /out/phase1_single/node0/slices

The `url_column` beside it was copied rather than linked, and survived. The
two differed in nothing else.

Rather than reason about which host directories a container happens to
mount, the test moves the staged tree and checks the slice is still readable
from its new location. A bind mount is the same problem: the tree appears at
a path it was not created at.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / (
    "stage_node_slices.sh"
)


def make_source(exp: Path, phase_slices: str = "slices_p1") -> None:
    src = exp / phase_slices
    src.mkdir(parents=True)
    (src / "slice_1.parquet").write_bytes(b"PAR1payload")
    (src / "slice_2.parquet").write_bytes(b"PAR1second")
    (src / "url_column").write_text("url\n")


def stage(exp: Path, phase: str, node: int, rel: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(exp), phase, str(node), rel],
        capture_output=True, text=True,
    )


def test_the_slice_is_staged_where_the_worker_looks(tmp_path) -> None:
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)

    result = stage(exp, "phase1_single", 0, "slices_p1/slice_1.parquet")
    assert result.returncode == 0, result.stderr

    staged = exp / "phase1_single" / "node0" / "slices"
    assert (staged / "slice_1.parquet").is_file()
    assert (staged / "url_column").read_text().strip() == "url"


def test_the_staged_slice_survives_the_tree_being_moved(tmp_path) -> None:
    """The regression test. A bind mount presents the tree at a new path.

    An absolute symlink points back at where the tree used to be, so it
    breaks — silently, as a file that is simply absent.
    """
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)
    assert stage(exp, "phase1_single", 0, "slices_p1/slice_1.parquet").returncode == 0

    moved = tmp_path / "mounted_elsewhere"
    shutil.move(str(exp), str(moved))

    staged = moved / "phase1_single" / "node0" / "slices" / "slice_1.parquet"
    assert staged.is_file(), (
        "the staged slice does not resolve from the new location; "
        "this is exactly what the worker saw through the bind mount"
    )
    assert staged.read_bytes() == b"PAR1payload"


def test_no_staged_path_escapes_the_experiment_tree(tmp_path) -> None:
    """Nothing under the node directory may refer outside it by absolute path."""
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)
    stage(exp, "phase1_single", 0, "slices_p1/slice_1.parquet")

    for path in (exp / "phase1_single").rglob("*"):
        if path.is_symlink():
            target = os.readlink(path)
            assert not target.startswith("/"), (
                f"{path} is an absolute symlink to {target}; it will not "
                "resolve through a bind mount"
            )


def test_each_node_gets_its_own_slice(tmp_path) -> None:
    """Two nodes staging different slices must not end up with the same one."""
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)

    stage(exp, "phase2_multi", 0, "slices_p1/slice_1.parquet")
    stage(exp, "phase2_multi", 1, "slices_p1/slice_2.parquet")

    base = exp / "phase2_multi"
    assert (base / "node0" / "slices" / "slice_1.parquet").read_bytes() \
        == b"PAR1payload"
    assert (base / "node1" / "slices" / "slice_1.parquet").read_bytes() \
        == b"PAR1second"


def test_staging_over_a_previous_run_s_leftovers(tmp_path) -> None:
    """A re-run finds the last run's files still in place.

    The first fix replaced a symlink with a copy, but `cp` refuses to write a
    file onto a symlink that points back at the source:

        cp: '.../slices_p1/slice_1.parquet' and
            '.../phase1_single/node0/slices/slice_1.parquet' are the same file

    Every phase of the second attempt failed on this, so the job again
    measured nothing. Staging has to be idempotent.
    """
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)

    stale = exp / "phase1_single" / "node0" / "slices"
    stale.mkdir(parents=True)
    (stale / "slice_1.parquet").symlink_to(exp / "slices_p1" / "slice_1.parquet")
    (stale / "url_column").write_text("stale\n")

    result = stage(exp, "phase1_single", 0, "slices_p1/slice_1.parquet")
    assert result.returncode == 0, result.stderr

    staged = stale / "slice_1.parquet"
    assert not staged.is_symlink(), "the stale link was not replaced"
    assert staged.read_bytes() == b"PAR1payload"
    assert (stale / "url_column").read_text().strip() == "url"


def test_a_missing_source_slice_fails_loudly(tmp_path) -> None:
    """Staging silently producing nothing is how this bug reached the cluster."""
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)

    result = stage(exp, "phase1_single", 0, "slices_p1/slice_9.parquet")
    assert result.returncode != 0
    assert "slice_9" in (result.stderr + result.stdout)


def test_a_missing_url_column_fails_loudly(tmp_path) -> None:
    """The worker needs it to pass --url_col; without it the run is wrong."""
    exp = tmp_path / "exp"
    exp.mkdir()
    make_source(exp)
    (exp / "slices_p1" / "url_column").unlink()

    result = stage(exp, "phase1_single", 0, "slices_p1/slice_1.parquet")
    assert result.returncode != 0
    assert "url_column" in (result.stderr + result.stdout)
